"""NAS Bridge Web 服务主入口"""
import os
import sys
import time
import yaml
import logging
import threading
import subprocess
from flask import Flask, render_template, jsonify, request, send_file

from db import Database
from sync_engine import SyncEngine
from watcher import Watcher

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)


def save_config():
    """将当前 config 字典写回 config.yaml"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False,
                  sort_keys=False)


def reload_sync_engine():
    """修改路径配置后，重新初始化 sync_engine 内部状态"""
    sync_engine.nas = config["nas"]
    sync_engine._dept_labels = config["nas"].get("production_labels", {})
    sync_engine._unc_map = config["nas"].get("unc_map", {})
    sync_engine._output_dir_cache.clear()

logging.basicConfig(
    level=getattr(logging, config.get("logging", {}).get("level", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            config.get("logging", {}).get("file", "nas_bridge.log"),
            encoding="utf-8"),
    ])

logger = logging.getLogger("nas-bridge")

db = Database(config.get("database", "nas_bridge.db"))
sync_engine = SyncEngine(config, db)
watcher = Watcher(config, db)

app = Flask(__name__)

# 开发模式：禁用浏览器缓存，避免修改代码后看到旧页面
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/projects")
def api_projects():
    return jsonify(sync_engine.get_projects_enriched())


@app.route("/api/scan", methods=["POST"])
def api_scan():
    names = sync_engine.scan_projects()
    sync_engine.clear_cache()
    return jsonify({
        "ok": True,
        "count": len(names),
        "projects": names,
    })


@app.route("/api/sync/<path:project_name>", methods=["POST"])
def api_sync(project_name):
    def _run():
        sync_engine.sync_project(project_name)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "同步已启动"})


@app.route("/api/deliver/<path:project_name>", methods=["POST"])
def api_deliver(project_name):
    data = request.get_json(silent=True) or {}
    file_path = data.get("file_path", "")
    ok, msg = sync_engine.deliver_file(project_name, file_path)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/deliver_batch/<path:project_name>", methods=["POST"])
def api_deliver_batch(project_name):
    data = request.get_json(silent=True) or {}
    file_names = data.get("file_names", [])
    if not file_names:
        return jsonify({"ok": False, "message": "未选择文件"})

    def _run():
        sync_engine.deliver_files_batch(project_name, file_names)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "批量回传已启动", "total": len(file_names)})


@app.route("/api/output_files/<path:project_name>")
def api_output_files(project_name):
    """列出成片文件，支持按模式列出（editing/revising/delivery）"""
    mode = request.args.get("mode", "editing")
    subpath = request.args.get("subpath", "")
    files = sync_engine.list_files_by_mode(project_name, mode, subpath)
    return jsonify(files)


@app.route("/api/logs")
def api_logs():
    limit = request.args.get("limit", 100, type=int)
    return jsonify(db.get_recent_logs(limit))


@app.route("/api/status")
def api_status():
    return jsonify({
        "watcher_enabled": watcher.enabled,
        "watched_dirs": len(watcher._watched_dirs),
        "production_roots": config["nas"].get("production_roots", []),
        "group_root": config["nas"].get("group_root", ""),
        "output_dir_name": config.get("output_dir_name", ""),
    })


@app.route("/api/project/<path:project_name>/progress")
def api_project_progress(project_name):
    """轻量端点：返回单个项目的同步/交付进度"""
    proj = db.get_project(project_name)
    if not proj:
        return jsonify({"ok": False, "message": "项目不存在"})
    return jsonify({
        "ok": True,
        "name": proj.get("name"),
        "sync_status": proj.get("sync_status", ""),
        "sync_progress": proj.get("sync_progress", ""),
        "delivery_status": proj.get("delivery_status", ""),
    })


@app.route("/api/project/<path:project_name>/dest_dir")
def api_project_dest_dir(project_name):
    """返回项目成片回传的目标目录路径"""
    path, err = sync_engine.get_dest_dir(project_name)
    if err:
        return jsonify({"ok": False, "message": err})
    return jsonify({"ok": True, "dest_dir": path})


@app.route("/api/project/<path:project_name>/source_dir")
def api_project_source_dir(project_name):
    """返回项目成片源目录路径（组内NAS侧）"""
    path, err = sync_engine.get_source_dir(project_name)
    if err:
        return jsonify({"ok": False, "message": err})
    return jsonify({"ok": True, "source_dir": path})


@app.route("/api/project/<path:project_name>/open_folder", methods=["POST"])
def api_project_open_folder(project_name):
    """在 Windows 资源管理器中打开指定目录
    which:
      - source: 组内NAS成片目录（01上映单集版）
      - dest:   制作部NAS成片目录（01上映单集版）
      - project: 项目根路径（优先 group_path，其次 production_path）
      - group:   组内NAS项目根目录
      - prod:    制作部NAS项目根目录
      - path:    直接打开请求体里 path 字段传的完整路径（不查 DB）
    可选: subpath - 拼到最终路径后面的相对子路径（如 "0810"）
    """
    data = request.get_json(silent=True) or {}
    which = data.get("which", "dest")
    subpath = (data.get("subpath") or "").replace("/", "\\").strip("\\/")

    path = None
    err = None
    if which == "path":
        path = (data.get("path") or "").strip()
        if not path:
            err = "未提供 path 参数"
    elif which in ("group", "prod", "project"):
        proj = db.get_project(project_name)
        if not proj:
            err = "项目不存在（未在数据库登记）"
        else:
            gp = proj.get("group_path", "") or ""
            pp = proj.get("production_path", "") or ""
            if which == "group":
                path = gp
                if not path:
                    err = "项目无组内NAS路径"
            elif which == "prod":
                path = pp
                if not path:
                    err = "项目无制作部NAS路径"
            else:
                # project: 优先 group_path
                path = gp or pp or None
                if not path:
                    err = "项目无可用路径"
    elif which == "source":
        path, err = sync_engine.get_source_dir(project_name)
    else:
        path, err = sync_engine.get_dest_dir(project_name)

    if err:
        return jsonify({"ok": False, "message": err})

    if subpath and path:
        path = os.path.join(path, subpath)

    if not path or not os.path.isdir(path):
        return jsonify({"ok": False, "message": "目录不存在: " + (path or "（空）")})
    try:
        os.startfile(path)
        return jsonify({"ok": True, "message": "已打开: " + path})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/project/<path:project_name>/custom_status", methods=["POST"])
def api_project_custom_status(project_name):
    """设置项目自定义状态（剪辑中/修改中/待交付/已完成）"""
    data = request.get_json(silent=True) or {}
    status = data.get("status", "")
    ok, msg = sync_engine.set_custom_status(project_name, status)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/project/<path:project_name>/episodes", methods=["POST"])
def api_project_episodes(project_name):
    """设置项目集数（总集数和当前已输出集数）"""
    data = request.get_json(silent=True) or {}
    total = data.get("total", 0)
    current = data.get("current", 0)
    ok, msg, completed = sync_engine.set_episodes(project_name, total, current)
    return jsonify({"ok": ok, "message": msg, "completed": completed})


@app.route("/api/project/<path:project_name>/auto_count")
def api_project_auto_count(project_name):
    """自动统计成片文件数量"""
    count = sync_engine.auto_count_episodes(project_name)
    return jsonify({"ok": True, "count": count})


@app.route("/api/project/<path:project_name>/delivery_stats", methods=["GET"])
def api_project_delivery_stats(project_name):
    """返回单个项目的交付统计（手动刷新用）"""
    stats = sync_engine.get_delivery_stats(project_name)
    return jsonify({"ok": True, "stats": stats})


@app.route("/api/project/<path:project_name>/deliver", methods=["POST"])
def api_project_deliver(project_name):
    """一键交付：把组内NAS项目下的 000交付 整个复制到制作部NAS对应项目"""
    ok, msg = sync_engine.deliver_to_production(project_name)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/project/<path:project_name>/deliver_status", methods=["GET"])
def api_project_deliver_status(project_name):
    """查询一键交付任务的实时状态"""
    status = sync_engine.get_deliver_status(project_name)
    return jsonify({"ok": True, "status": status})


@app.route("/api/deliver_revision/<path:project_name>", methods=["POST"])
def api_deliver_revision(project_name):
    """修改模式下批量回传文件到制作部NAS的修改文件夹"""
    data = request.get_json(silent=True) or {}
    file_names = data.get("file_names", [])
    rev_folder_name = data.get("rev_folder_name")
    if not file_names:
        return jsonify({"ok": False, "message": "未选择文件"})

    def _run():
        sync_engine.deliver_revision_batch(project_name, file_names, rev_folder_name)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "修改回传已启动", "total": len(file_names)})


@app.route("/api/deliver_revision_single/<path:project_name>", methods=["POST"])
def api_deliver_revision_single(project_name):
    """修改模式下单个文件回传（同步）"""
    data = request.get_json(silent=True) or {}
    file_name = data.get("file_name", "")
    rev_folder_name = data.get("rev_folder_name")
    if not file_name:
        return jsonify({"ok": False, "message": "未指定文件"})
    ok, msg = sync_engine.deliver_revision_file(project_name, file_name, rev_folder_name)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/deliver_revision_folders/<path:project_name>", methods=["POST"])
def api_deliver_revision_folders(project_name):
    """修改模式下批量回传整个文件夹到制作部NAS"""
    data = request.get_json(silent=True) or {}
    folder_names = data.get("folder_names", [])
    if not folder_names:
        return jsonify({"ok": False, "message": "未选择文件夹"})

    def _run():
        sync_engine.deliver_revision_folders_batch(project_name, folder_names)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "文件夹回传已启动", "total": len(folder_names)})


@app.route("/api/preview/<path:project_name>/<path:filename>")
def api_preview_file(project_name, filename):
    """预览成片文件：流式返回文件内容，支持 Range 请求（视频拖拽）。
    支持 mode 参数指定文件来源模式。
    """
    mode = request.args.get("mode", "editing")
    subpath = request.args.get("subpath", "")
    file_path = sync_engine.get_file_path_for_preview(project_name, filename, mode, subpath)
    if file_path and os.path.isfile(file_path):
        try:
            return send_file(file_path, conditional=True)
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)}), 500
    return jsonify({"ok": False, "message": "文件不存在"}), 404


@app.route("/api/debug/sort")
def api_debug_sort():
    """调试端点：验证自然排序是否生效"""
    from sync_engine import _natural_key
    test = ["60.mp4", "10.mp4", "2.mp4", "1.mp4", "11.mp4"]
    sorted_list = sorted(test, key=_natural_key)
    return jsonify({
        "natural_key_available": True,
        "input": test,
        "sorted": sorted_list,
        "expected": ["1.mp4", "2.mp4", "10.mp4", "11.mp4", "60.mp4"],
    })


# ==================== NAS 路径管理 ====================

@app.route("/api/config/paths")
def api_get_paths():
    """获取当前所有 NAS 路径配置"""
    nas = config.get("nas", {})
    return jsonify({
        "ok": True,
        "production_roots": nas.get("production_roots", []),
        "production_labels": nas.get("production_labels", {}),
        "group_root": nas.get("group_root", ""),
        "unc_map": nas.get("unc_map", {}),
    })


@app.route("/api/config/paths", methods=["POST"])
def api_add_path():
    """新增 NAS 路径
    body: { type: "production"|"group"|"unc", path, label, drive, unc }
    """
    data = request.get_json(silent=True) or {}
    ptype = data.get("type", "")
    nas = config.setdefault("nas", {})

    if ptype == "production":
        path = (data.get("path") or "").strip()
        label = (data.get("label") or "").strip()
        if not path:
            return jsonify({"ok": False, "message": "路径不能为空"})
        # 标准化路径分隔符
        path = path.replace("/", "\\").rstrip("\\")
        roots = nas.setdefault("production_roots", [])
        if path in roots:
            return jsonify({"ok": False, "message": "该路径已存在"})
        roots.append(path)
        if label:
            labels = nas.setdefault("production_labels", {})
            labels[path] = label
        save_config()
        reload_sync_engine()
        logger.info("新增制作部 NAS 路径: %s (标签: %s)", path, label)
        return jsonify({"ok": True, "message": "已添加制作部路径: " + path})

    elif ptype == "group":
        path = (data.get("path") or "").strip()
        if not path:
            return jsonify({"ok": False, "message": "路径不能为空"})
        path = path.replace("/", "\\").rstrip("\\")
        nas["group_root"] = path
        save_config()
        reload_sync_engine()
        logger.info("更新组内 NAS 路径: %s", path)
        return jsonify({"ok": True, "message": "已更新组内 NAS 路径: " + path})

    elif ptype == "unc":
        drive = (data.get("drive") or "").strip().upper()
        unc = (data.get("unc") or "").strip()
        if not drive or not unc:
            return jsonify({"ok": False, "message": "盘符和 UNC 路径不能为空"})
        if not drive.endswith(":"):
            drive = drive + ":"
        unc = unc.replace("/", "\\").rstrip("\\")
        unc_map = nas.setdefault("unc_map", {})
        unc_map[drive] = unc
        save_config()
        reload_sync_engine()
        logger.info("新增 UNC 映射: %s -> %s", drive, unc)
        return jsonify({"ok": True, "message": "已添加 UNC 映射: " + drive + " -> " + unc})

    else:
        return jsonify({"ok": False, "message": "未知路径类型: " + str(ptype)})


@app.route("/api/config/paths", methods=["DELETE"])
def api_remove_path():
    """删除 NAS 路径
    body: { type: "production"|"unc", path, drive }
    """
    data = request.get_json(silent=True) or {}
    ptype = data.get("type", "")
    nas = config.get("nas", {})

    if ptype == "production":
        path = (data.get("path") or "").strip()
        path = path.replace("/", "\\").rstrip("\\")
        roots = nas.get("production_roots", [])
        if path in roots:
            roots.remove(path)
            labels = nas.get("production_labels", {})
            if path in labels:
                del labels[path]
            save_config()
            reload_sync_engine()
            logger.info("删除制作部 NAS 路径: %s", path)
            return jsonify({"ok": True, "message": "已删除: " + path})
        return jsonify({"ok": False, "message": "路径不存在"})

    elif ptype == "unc":
        drive = (data.get("drive") or "").strip().upper()
        if not drive.endswith(":"):
            drive = drive + ":"
        unc_map = nas.get("unc_map", {})
        if drive in unc_map:
            del unc_map[drive]
            save_config()
            reload_sync_engine()
            logger.info("删除 UNC 映射: %s", drive)
            return jsonify({"ok": True, "message": "已删除 UNC 映射: " + drive})
        return jsonify({"ok": False, "message": "UNC 映射不存在"})

    else:
        return jsonify({"ok": False, "message": "未知路径类型"})


# ==================== 服务管理 ====================

@app.route("/api/service/stop", methods=["POST"])
def api_service_stop():
    """停止 NAS Bridge 服务"""
    def _do_stop():
        time.sleep(0.8)
        try:
            watcher.stop()
        except Exception:
            pass
        logger.info("服务已停止（来自前端请求）")
        os._exit(0)

    threading.Thread(target=_do_stop, daemon=True).start()
    return jsonify({"ok": True, "message": "服务即将停止，窗口将在 1 秒后关闭"})


@app.route("/api/service/restart", methods=["POST"])
def api_service_restart():
    """重启 NAS Bridge 服务"""
    def _do_restart():
        time.sleep(0.8)
        try:
            watcher.stop()
        except Exception:
            pass
        python_exe = sys.executable
        if python_exe.lower().endswith("python.exe"):
            pythonw = python_exe[:-10] + "pythonw.exe"
        else:
            pythonw = python_exe
        if not os.path.isfile(pythonw):
            pythonw = python_exe

        app_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS

        try:
            subprocess.Popen(
                [pythonw, app_py],
                cwd=os.path.dirname(app_py),
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
            logger.info("服务重启中（pid=%s），新进程已拉起", os.getpid())
        except Exception as e:
            logger.error("重启失败: %s", e)
        os._exit(0)

    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify({"ok": True, "message": "服务即将重启，页面将自动刷新"})


def main():
    web_cfg = config.get("web", {})
    host = web_cfg.get("host", "0.0.0.0")
    port = web_cfg.get("port", 8080)
    logger.info("Web 服务启动: http://%s:%d", host, port)

    # 先启动 Flask，再在后台线程启动 watcher（扫描大量目录可能很慢）
    threading.Thread(target=watcher.start, daemon=True).start()

    try:
        app.run(host=host, port=port, debug=False)
    finally:
        watcher.stop()


if __name__ == "__main__":
    main()
