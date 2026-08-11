"""素材同步引擎 - 支持多制作部源 + 递归查找成片目录"""
import os
import shutil
import subprocess
import logging
import threading
from datetime import datetime

logger = logging.getLogger(__name__)


def _natural_key(text):
    """自然排序键：将数字段转为整数，使 '2' 排在 '10' 前面。"""
    import re
    parts = re.split(r'(\d+)', str(text))
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def find_dir_recursive(base_path, target_name, max_depth=6):
    """递归搜索目录树中名为 target_name 的目录，返回所有匹配的绝对路径列表。
    这是整个方案的核心工具函数：不假定输出目录在固定位置，而是逐层搜索。
    """
    found = []
    try:
        for entry in os.scandir(base_path):
            if entry.is_dir():
                if entry.name == target_name:
                    found.append(entry.path)
                elif max_depth > 0:
                    found.extend(
                        find_dir_recursive(entry.path, target_name,
                                           max_depth - 1))
    except PermissionError:
        pass
    except OSError:
        pass
    return found


def _quick_find_file(base_path, filename, max_depth=4, timeout=2.0):
    """浅层快速查找：在 max_depth 层内用 os.scandir 找文件名，超过 timeout 秒直接放弃。
    避免在大的网络盘上 os.walk 阻塞 API。
    """
    import time as _time
    deadline = _time.time() + timeout

    def _walk(depth, cur):
        if _time.time() > deadline:
            return None
        try:
            with os.scandir(cur) as entries:
                for e in entries:
                    if e.is_file(follow_symlinks=False) and e.name == filename:
                        return e.path
                    if e.is_dir(follow_symlinks=False) and depth > 0:
                        found = _walk(depth - 1, e.path)
                        if found:
                            return found
        except (PermissionError, OSError):
            pass
        return None

    return _walk(max_depth, base_path)


# ==================== subprocess 常量 & 工具 ====================

# robocopy 基础参数（所有复制场景共享）
ROBOCOPY_BASE = ["/E", "/R:1", "/W:1", "/NP", "/NFL", "/NDL"]
ROBOCOPY_MIR = ["/MIR"] + ROBOCOPY_BASE
ROBOCOPY_FAST = ["/MT:8"] + ROBOCOPY_BASE       # 多线程快速复制
ROBOCOPY_XCOPY_CMD = ["cmd", "/c", "xcopy", "/E", "/I", "/Y"]
CMD_MKDIR_CMD = ["cmd", "/c", "mkdir"]

# 超时常量（秒）
TIMEOUT_MKDIR = 30
TIMEOUT_XCOPY_SMALL = 120
TIMEOUT_XCOPY_BIG = 600
TIMEOUT_ROBOCOPY_FAST = 3600      # 一键交付 → 制作部
TIMEOUT_ROBOCOPY_SYNC = 7200      # 组内 → 制作部同步


def _exec(cmd, timeout, label="", unc_alt=None):
    """统一 subprocess.run 封装。
    cmd: [exe, arg1, ...]
    timeout: 超时秒
    label: 日志标签
    unc_alt: 如果 timeout 触发，用 UNC 路径的备选命令 (list)
    返回 (ok: bool, msg: str, returncode: int)
    """
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        rc = result.returncode
        if rc < 8 and rc != -9:
            return True, "ok", rc
        err = result.stderr.decode("gbk", errors="replace")[:500] \
            if result.stderr else "rc=%d" % rc
        return False, err, rc
    except subprocess.TimeoutExpired:
        if unc_alt:
            logger.warning("%s 超时，尝试 UNC 备选", label)
            try:
                result = subprocess.run(unc_alt, capture_output=True, timeout=timeout)
                rc = result.returncode
                if rc < 8:
                    return True, "ok (unc)", rc
                err = result.stderr.decode("gbk", errors="replace")[:500] \
                    if result.stderr else "rc=%d" % rc
                return False, "UNC 备选失败: " + err, rc
            except Exception as e:
                return False, "UNC 备选异常: " + str(e), -1
        return False, "超时（超过 %d 秒）" % timeout, -1
    except Exception as e:
        return False, str(e), -1


def _exec_popen(cmd, label=""):
    """启动 robocopy Popen 句柄，返回 (proc, pid)。"""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc, proc.pid


class SyncEngine:
    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.nas = config["nas"]
        self.sync_cfg = config.get("sync", {})
        self.output_dir_name = config.get("output_dir_name",
                                          "01上映单集版")
        self.special_projects = config.get("special_projects", {}) or {}
        self._output_dir_cache = {}  # 缓存递归查找结果
        self._deliver_tasks = {}      # project_name -> {status, total, current, pct, started_at, message}
        self._lock = threading.RLock()  # 保护上面两个共享字典
        self._dept_labels = config["nas"].get("production_labels", {})
        self._unc_map = config["nas"].get("unc_map", {})
        self._delivery_check_running = False  # 防止重复后台检测
        self._delivery_folder = config.get(
            "delivery_folder", r"C:\Users\Admin\Desktop\000交付")

    # ==================== UNC 路径转换 ====================

    def _to_unc(self, path):
        """将盘符路径转为 UNC 路径（管理员进程用 cmd+UNC 绕过权限隔离）"""
        path = path.replace("/", "\\")
        for drive, unc in self._unc_map.items():
            d = drive.rstrip(":") + ":"
            if path.upper().startswith(d.upper()):
                rest = path[len(d):].lstrip("\\")
                return os.path.join(unc, rest)
        return path

    # ==================== 部门标签 ====================

    def _get_department_label(self, source_root):
        """从制作部源路径提取部门标签"""
        if not source_root:
            return None
        normalized = source_root.replace("/", "\\")
        return self._dept_labels.get(normalized,
                                     os.path.basename(source_root))

    # ==================== 项目扫描 ====================

    def scan_projects(self):
        """扫描所有制作部 NAS 源中的项目列表，写入数据库。
        自动识别月份子目录（如 7月/8月），进入下一层扫描实际项目。
        """
        roots = self.nas.get("production_roots", [])
        if not roots:
            logger.error("未配置制作部 NAS 路径 (production_roots)")
            return []

        import re
        month_pattern = re.compile(r'^\d{1,2}月$')

        all_names = []
        for root in roots:
            if not os.path.isdir(root):
                logger.warning("制作部 NAS 路径不存在，跳过: %s", root)
                continue

            # 先看第一层是否都是月份目录，如果是则进入第二层
            try:
                entries = os.listdir(root)
            except OSError:
                continue

            dirs = [e for e in entries
                    if os.path.isdir(os.path.join(root, e))]
            month_dirs = [d for d in dirs if month_pattern.match(d)]

            if month_dirs and len(month_dirs) / max(len(dirs), 1) > 0.3:
                # 大部分子目录是月份 → 进入每个月目录扫描实际项目
                logger.info("检测到月份子目录结构: %s，进入深层扫描", root)
                scan_dirs = [os.path.join(root, md) for md in month_dirs]
            else:
                # 正常结构 → 第一层就是项目
                scan_dirs = [root]

            for parent in scan_dirs:
                try:
                    children = os.listdir(parent)
                except OSError:
                    continue
                for name in children:
                    full = os.path.join(parent, name)
                    if not os.path.isdir(full):
                        continue
                    # 跳过模板/配置目录（00开头的一般是模板）
                    if name.startswith("00"):
                        continue
                    if month_pattern.match(name):
                        continue
                    is_special = name in self.special_projects
                    sc = self.special_projects.get(name, {})
                    group_path = os.path.join(self.nas["group_root"], name)
                    self.db.upsert_project(
                        name, full, group_path,
                        source_root=root,
                        is_special=1 if is_special else 0,
                        special_config=sc)
                    all_names.append(name)

        logger.info("从 %d 个制作部源扫描到 %d 个项目",
                    len(roots), len(all_names))
        return all_names

    # ==================== 素材同步 ====================

    def sync_project(self, project_name):
        """将单个项目完整从制作部 NAS 同步到组内 NAS"""
        proj = self.db.get_project(project_name)
        if not proj:
            return False, "项目不存在"

        src_root = proj["production_path"]
        dst_root = proj["group_path"]

        self.db.update_project_status(
            project_name, sync_status="syncing", sync_progress="准备中...")
        self.db.add_sync_log(
            project_name, "开始同步", "production->group",
            status="info", message="源: " + src_root)

        os.makedirs(dst_root, exist_ok=True)

        sync_mode = self.sync_cfg.get("mode", "full")
        exclude = self.sync_cfg.get("exclude_patterns", [])

        if sync_mode == "partial":
            sync_subdirs = self.sync_cfg.get("sync_subdirs", [])
            if sync_subdirs:
                total = len(sync_subdirs)
                for i, subdir in enumerate(sync_subdirs, 1):
                    src = os.path.join(src_root, subdir)
                    dst = os.path.join(dst_root, subdir)
                    if not os.path.isdir(src):
                        logger.warning("源子目录不存在，跳过: %s", src)
                        continue
                    progress = "(%d/%d) 同步: %s" % (i, total, subdir)
                    self.db.update_project_status(
                        project_name, sync_progress=progress)
                    self.db.add_sync_log(
                        project_name, "同步子目录", "production->group",
                        file_path=subdir, status="info", message=progress)
                    ok, msg = self._robocopy(src, dst, exclude)
                    if not ok:
                        self.db.add_sync_log(
                            project_name, "同步失败", "production->group",
                            file_path=subdir, status="error", message=msg)
            else:
                # partial mode without subdirs → sync entire project
                self.db.update_project_status(
                    project_name, sync_progress="同步整个项目目录...")
                ok, msg = self._robocopy(src_root, dst_root, exclude)
                if not ok:
                    self.db.add_sync_log(
                        project_name, "同步失败", "production->group",
                        status="error", message=msg)
        else:
            # full mode — mirror entire project
            self.db.update_project_status(
                project_name, sync_progress="完整同步项目目录...")
            ok, msg = self._robocopy(src_root, dst_root, exclude)
            if not ok:
                self.db.add_sync_log(
                    project_name, "同步失败", "production->group",
                    status="error", message=msg)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.db.update_project_status(
            project_name, sync_status="synced",
            sync_progress="", last_synced_at=now)
        self.db.add_sync_log(
            project_name, "同步完成", "production->group",
            status="success", message="所有素材同步完成")
        return True, "同步完成"

    def _robocopy(self, src, dst, exclude_patterns):
        """执行 robocopy 增量镜像同步"""
        cmd = ["robocopy", src, dst] + ROBOCOPY_MIR

        xf = [p for p in exclude_patterns
              if "*" in p or "." in p]
        xd = [p for p in exclude_patterns
              if "*" not in p and "." not in p]
        if xf:
            cmd += ["/XF"] + xf
        if xd:
            cmd += ["/XD"] + xd

        ok, msg, rc = _exec(cmd, TIMEOUT_ROBOCOPY_SYNC, label="robocopy_sync")
        if ok:
            return True, "同步成功"
        return False, "robocopy 返回码 %d: %s" % (rc, msg)

    # ==================== 成片交付 ====================

    def get_dest_dir(self, project_name):
        """获取项目的成片回传目标目录（制作部NAS侧的01上映单集版）"""
        proj = self.db.get_project(project_name)
        if not proj:
            return None, "项目不存在"
        prod_path = proj.get("production_path", "")
        if not prod_path:
            return None, "该项目无制作部路径（仅组内项目）"
        dirs = self._find_output_dirs(prod_path, project_name)
        if not dirs:
            return None, "制作部项目中未找到 %s 目录" % self._get_output_dir_name(project_name)
        return dirs[0], None

    def get_source_dir(self, project_name):
        """获取项目的成片源目录（组内NAS侧的01上映单集版）"""
        proj = self.db.get_project(project_name)
        if not proj:
            return None, "项目不存在"
        group_path = proj.get("group_path", "")
        if not group_path:
            return None, "该项目无组内路径"
        dirs = self._find_output_dirs(group_path, project_name)
        if not dirs:
            return None, "组内项目中未找到 %s 目录" % self._get_output_dir_name(project_name)
        return dirs[0], None

    def _get_output_dir_name(self, project_name):
        """获取项目的成片输出目录名"""
        if project_name in self.special_projects:
            return self.special_projects[project_name].get(
                "output_dir_name", self.output_dir_name)
        return self.output_dir_name

    def _find_output_dirs(self, base_path, project_name):
        """在项目目录下递归查找 01上映单集版 目录，带缓存"""
        cache_key = base_path + "|" + project_name
        with self._lock:
            if cache_key in self._output_dir_cache:
                return self._output_dir_cache[cache_key]

        dir_name = self._get_output_dir_name(project_name)
        dirs = find_dir_recursive(base_path, dir_name)

        with self._lock:
            self._output_dir_cache[cache_key] = dirs
        return dirs

    def deliver_file(self, project_name, file_path):
        """手动回传成片：从组内NAS 01上映单集版 → 制作部NAS对应项目的01上映单集版"""
        proj = self.db.get_project(project_name)
        if not proj:
            return False, "项目不存在"

        # 如果传了绝对路径直接使用；否则在 group 项目下查找
        src = file_path
        if not os.path.isabs(file_path):
            # 在组内项目目录下递归找 01上映单集版
            group_output_dirs = self._find_output_dirs(
                proj["group_path"], project_name)
            for od in group_output_dirs:
                candidate = os.path.join(od, file_path)
                if os.path.isfile(candidate):
                    src = candidate
                    break
            else:
                # 简单兜底：在 group_path 下浅层（4 层内）快速找文件名，超时或找不到直接让用户用完整路径
                found = _quick_find_file(proj["group_path"], file_path, max_depth=4)
                if not found:
                    return False, "未在组内项目目录中找到文件: " + file_path + \
                        "，请使用完整路径重试"
                src = found

        if not os.path.isfile(src):
            return False, "文件不存在: " + src

        filename = os.path.basename(src)

        # 在制作部项目目录下递归查找 01上映单集版
        prod_output_dirs = self._find_output_dirs(
            proj["production_path"], project_name)

        if not prod_output_dirs:
            return False, "制作部项目目录中未找到 %s 目录" % \
                self._get_output_dir_name(project_name)

        # 使用第一个匹配的制作部输出目录
        dst_dir = prod_output_dirs[0]
        dst = os.path.join(dst_dir, filename)

        try:
            file_size = os.path.getsize(src)
            os.makedirs(dst_dir, exist_ok=True)

            last_err = None

            # 方案1：直接 Python copy（net use 成功后可用）
            try:
                shutil.copy(src, dst)
                last_err = None  # success
            except PermissionError as e:
                last_err = str(e)
            except OSError as e:
                last_err = str(e)

            # 方案2：fallback 到 cmd copy + UNC 路径
            if last_err is not None:
                src_unc = self._to_unc(src)
                dst_unc = self._to_unc(dst)
                result = subprocess.run(
                    ["cmd", "/c", "copy", "/y", src_unc, dst_unc],
                    capture_output=True, timeout=120)
                if result.returncode != 0:
                    err = (result.stderr or result.stdout or b"Unknown error")
                    # cmd 输出是 GBK 编码
                    err_str = err.decode("gbk", errors="replace").strip()
                    raise OSError(err_str)
                # cmd copy 成功，确认文件存在
                if not os.path.isfile(dst):
                    raise OSError("cmd copy reported success but file not found: " + dst)

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.db.add_delivery_log(
                project_name, filename, src, dst, file_size,
                "success", "手动回传成功")
            self.db.update_project_status(
                project_name, delivery_status="delivered",
                last_delivered_at=now)
            return True, "回传成功: " + dst
        except Exception as e:
            self.db.add_delivery_log(
                project_name, filename, src, dst, 0,
                "error", str(e))
            return False, str(e)

    def deliver_files_batch(self, project_name, file_names):
        """批量回传成片文件，返回每个文件的回传结果"""
        results = []
        total = len(file_names)
        success_count = 0
        fail_count = 0

        # 初始进度：回传 0/total
        self.db.update_project_status(
            project_name,
            delivery_status="delivering",
            sync_progress="回传 0/{}".format(total))

        for i, fname in enumerate(file_names):
            ok, msg = self.deliver_file(project_name, fname)
            if ok:
                success_count += 1
            else:
                fail_count += 1
            results.append({
                "name": fname,
                "ok": ok,
                "message": msg,
                "index": i + 1,
                "total": total,
            })
            # 即时写入进度：格式必须与前端正则 /回传\s+(\d+)\/(\d+)/ 匹配
            self.db.update_project_status(
                project_name,
                delivery_status="delivering",
                sync_progress="回传 {}/{}".format(i + 1, total))

        # 全部完成后，只有至少成功 1 个才标记为已交付
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if success_count > 0:
            self.db.update_project_status(
                project_name, delivery_status="delivered",
                sync_progress="", last_delivered_at=now)
        else:
            self.db.update_project_status(
                project_name, delivery_status="error",
                sync_progress="全部失败 0/{}".format(total),
                last_delivered_at="")
        return results

    def list_output_files(self, project_name):
        """列出组内NAS项目所有 01上映单集版 目录中的文件"""
        proj = self.db.get_project(project_name)
        if not proj:
            return []

        output_dirs = self._find_output_dirs(
            proj["group_path"], project_name)

        if not output_dirs:
            return []

        files = []
        for od in output_dirs:
            try:
                for name in os.listdir(od):
                    full = os.path.join(od, name)
                    if os.path.isfile(full):
                        ext = os.path.splitext(name)[1].lower()
                        size = os.path.getsize(full)
                        files.append({
                            "name": name,
                            "path": full,
                            "size": size,
                            "size_mb": round(size / 1024 / 1024, 1),
                            "ext": ext,
                            "mtime": datetime.fromtimestamp(
                                os.path.getmtime(full)).strftime(
                                "%Y-%m-%d %H:%M"),
                            "parent_dir": os.path.basename(od) if od else ""
                        })
            except OSError:
                continue

        files.sort(key=lambda x: _natural_key(x["name"]))
        return files

    # ==================== 组盘检测与扫描 ====================

    def check_group_existence(self):
        """检查所有制作部项目在组内 NAS 上是否已存在"""
        group_root = self.nas["group_root"]
        if not os.path.isdir(group_root):
            return

        projects = self.db.get_all_projects()
        for proj in projects:
            group_path = os.path.join(group_root, proj["name"])
            exists = os.path.isdir(group_path)
            # 更新数据库（这里用 extra 字段记录，但我们直接改API层返回）
            proj["on_group"] = exists

    def scan_group_projects(self):
        """扫描组内 NAS 上全部项目目录，写入数据库（标记为 group_only 类型）"""
        group_root = self.nas["group_root"]
        if not os.path.isdir(group_root):
            logger.warning("组内 NAS 路径不存在: %s", group_root)
            return []

        import re
        month_pattern = re.compile(r'^\d{1,2}月$')

        # 先清理旧数据中 source_root 为空的（O盘项目），准备重新扫描
        all_projects = self.db.get_all_projects()
        for proj in all_projects:
            if not proj.get("source_root"):
                self.db.delete_project(proj["name"])

        found = []
        try:
            for name in os.listdir(group_root):
                full = os.path.join(group_root, name)
                if not os.path.isdir(full):
                    continue
                if name.startswith("00"):
                    continue
                if month_pattern.match(name):
                    continue

                # 写入数据库：source_root 为空 = group_only 类型
                self.db.upsert_project(
                    name, "", full,
                    source_root="",
                    is_special=0,
                    special_config={})
                found.append(name)
        except OSError as e:
            logger.error("扫描组内 NAS 失败: %s", e)

        logger.info("组内 NAS 扫描到 %d 个项目", len(found))
        return found

    def check_delivery_status(self, project_name):
        """比较组内NAS和制作部NAS的成片目录文件列表，自动判断交付状态。
        返回 'delivered' / 'partial' / 'pending' / None（无法判断）
        """
        proj = self.db.get_project(project_name)
        if not proj:
            return None

        prod_path = proj.get("production_path", "")
        group_path = proj.get("group_path", "")

        if not prod_path or not group_path:
            return None

        source_dirs = self._find_output_dirs(group_path, project_name)
        if not source_dirs:
            return None  # 组内没有成片目录

        # 获取源文件名集合
        source_files = set()
        for sd in source_dirs:
            try:
                for name in os.listdir(sd):
                    if os.path.isfile(os.path.join(sd, name)):
                        source_files.add(name)
            except OSError:
                continue

        if not source_files:
            return None  # 没有成片文件

        dest_dirs = self._find_output_dirs(prod_path, project_name)
        if not dest_dirs:
            return "pending"  # 制作部没有成片目录

        # 获取目标文件名集合
        dest_files = set()
        for dd in dest_dirs:
            try:
                for name in os.listdir(dd):
                    if os.path.isfile(os.path.join(dd, name)):
                        dest_files.add(name)
            except OSError:
                continue

        # 检查所有源文件是否都存在于目标
        if source_files.issubset(dest_files):
            return "delivered"
        elif source_files & dest_files:
            return "partial"
        else:
            return "pending"

    def get_projects_enriched(self):
        """获取所有项目，附带部门标签和组盘标记。
        返回 { production: [...], group_all: [...] }
        group_all 包含 O 盘上所有项目目录（无论制作部是否有同名项目）。
        交付状态从数据库读取，后台异步检测更新（不阻塞 API 响应）。
        """
        group_root = self.nas["group_root"]
        production = []
        group_all = []

        # 从数据库获取所有项目，建立名称索引
        db_projects = {}
        for proj in self.db.get_all_projects():
            db_projects[proj["name"]] = proj

        # 预先扫描 00已完成 目录名，用于过滤 production（手动归档的项目不要重复出现）
        completed_names = set()
        completed_root = os.path.join(group_root, "00已完成")
        if os.path.isdir(completed_root):
            try:
                for _cn in os.listdir(completed_root):
                    if os.path.isdir(os.path.join(completed_root, _cn)):
                        completed_names.add(_cn)
            except OSError:
                pass

        for proj in db_projects.values():
            if proj.get("source_root"):
                custom_status = proj.get("custom_status", "") or ""
                if custom_status == "已完成":
                    continue
                # 目录已被手动移入 00已完成（项目名在 completed_names 里），也跳过
                if proj["name"] in completed_names:
                    continue
                dept = self._get_department_label(proj["source_root"])
                proj["department"] = dept
                proj["project_type"] = "production"
                proj["custom_status"] = custom_status
                proj["total_episodes"] = proj.get("total_episodes", 0) or 0
                proj["current_episodes"] = proj.get("current_episodes", 0) or 0
                production.append(proj)

        # 生产部项目名集合，用于交叉对照
        prod_names = {p["name"] for p in production}

        # 扫描 O 盘全部项目目录（实时）
        import re
        month_pattern = re.compile(r'^\d{1,2}月$')
        if os.path.isdir(group_root):
            for name in os.listdir(group_root):
                full = os.path.join(group_root, name)
                if not os.path.isdir(full):
                    continue
                if name.startswith("00") or month_pattern.match(name):
                    continue

                # 从数据库获取项目状态（delivery_status 等）
                db_proj = db_projects.get(name)
                # 状态已完成的项目跳过（会单独在 group_completed 展示）
                if db_proj and (db_proj.get("custom_status", "") or "") == "已完成":
                    continue
                entry = {
                    "name": name,
                    "group_path": full,
                    "department": "组内NAS",
                    "source_department": "",
                    "project_type": "group",
                    "has_production_match": name in prod_names,
                    "delivery_status": "pending",
                    "last_delivered_at": "",
                    "sync_status": "pending",
                    "last_synced_at": "",
                    "sync_progress": "",
                    "is_special": 0,
                    "custom_status": "",
                    "created_at": "",
                    "total_episodes": 0,
                    "current_episodes": 0,
                }
                if db_proj:
                    entry["delivery_status"] = db_proj.get("delivery_status", "pending")
                    entry["last_delivered_at"] = db_proj.get("last_delivered_at") or ""
                    entry["sync_status"] = db_proj.get("sync_status", "pending")
                    entry["last_synced_at"] = db_proj.get("last_synced_at") or ""
                    entry["sync_progress"] = db_proj.get("sync_progress") or ""
                    entry["is_special"] = db_proj.get("is_special", 0)
                    entry["custom_status"] = db_proj.get("custom_status", "") or ""
                    entry["created_at"] = db_proj.get("created_at") or ""
                    entry["total_episodes"] = db_proj.get("total_episodes", 0) or 0
                    entry["current_episodes"] = db_proj.get("current_episodes", 0) or 0

                group_all.append(entry)

        group_all.sort(key=lambda x: _natural_key(x["name"]))

        # 标记 production 项目在组盘是否存在
        group_names = {g["name"] for g in group_all}
        for proj in production:
            proj["on_group"] = proj["name"] in group_names

        # 同步 group_all 条目的交付状态（从 production 数据读取）
        prod_status_map = {p["name"]: p for p in production}
        for g in group_all:
            if g["name"] in prod_status_map:
                p = prod_status_map[g["name"]]
                g["delivery_status"] = p.get("delivery_status", "pending")
                g["last_delivered_at"] = p.get("last_delivered_at", "")
                g["custom_status"] = p.get("custom_status", "") or ""
                g["source_department"] = p.get("department", "") or ""
                g["total_episodes"] = p.get("total_episodes", 0) or 0
                g["current_episodes"] = p.get("current_episodes", 0) or 0
                if not g.get("created_at"):
                    g["created_at"] = p.get("created_at", "") or ""

        # 后台异步检测交付状态（不阻塞 API 响应）
        self._start_delivery_check_background(production)

        # 扫描 00已完成 子目录
        group_completed = []
        completed_root = os.path.join(group_root, "00已完成")
        if os.path.isdir(completed_root):
            for name in os.listdir(completed_root):
                full = os.path.join(completed_root, name)
                if not os.path.isdir(full):
                    continue
                db_proj = db_projects.get(name)
                entry = {
                    "name": name,
                    "group_path": full,
                    "department": "已完成",
                    "source_department": "",
                    "project_type": "group",
                    "has_production_match": name in prod_names,
                    "delivery_status": "pending",
                    "last_delivered_at": "",
                    "sync_status": "pending",
                    "last_synced_at": "",
                    "sync_progress": "",
                    "is_special": 0,
                    "custom_status": "已完成",
                    "created_at": "",
                    "total_episodes": 0,
                    "current_episodes": 0,
                    "is_completed": True,
                }
                if db_proj:
                    entry["delivery_status"] = db_proj.get("delivery_status", "pending")
                    entry["last_delivered_at"] = db_proj.get("last_delivered_at") or ""
                    entry["sync_status"] = db_proj.get("sync_status", "pending")
                    entry["last_synced_at"] = db_proj.get("last_synced_at") or ""
                    entry["sync_progress"] = db_proj.get("sync_progress") or ""
                    entry["custom_status"] = db_proj.get("custom_status", "") or "已完成"
                    entry["created_at"] = db_proj.get("created_at") or ""
                    entry["total_episodes"] = db_proj.get("total_episodes", 0) or 0
                    entry["current_episodes"] = db_proj.get("current_episodes", 0) or 0
                    source_dept = db_proj.get("department", "") or ""
                    if source_dept:
                        entry["source_department"] = source_dept
                group_completed.append(entry)

        group_completed.sort(key=lambda x: _natural_key(x["name"]))

        # 给待交付 / 已完成项目附加交付统计
        for bucket in (production, group_all, group_completed):
            for proj in bucket:
                if proj.get("custom_status") in ("待交付", "已完成"):
                    try:
                        proj["delivery_stats"] = self.get_delivery_stats(proj["name"])
                    except Exception:
                        proj["delivery_stats"] = {"found": False, "total_episodes": 0, "items": [], "overall_pct": 0}

        return {
            "production": production,
            "group_all": group_all,
            "group_completed": group_completed,
        }

    def _start_delivery_check_background(self, production):
        """在后台线程中检测交付状态，避免阻塞 API 响应"""
        if self._delivery_check_running:
            return
        self._delivery_check_running = True

        def _check():
            try:
                for proj in production:
                    name = proj["name"]
                    current_status = proj.get("delivery_status", "pending")
                    if current_status == "delivering":
                        continue  # 正在回传中，跳过
                    auto_status = self.check_delivery_status(name)
                    if auto_status and auto_status != current_status:
                        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        delivered_at = proj.get("last_delivered_at") or ""
                        if auto_status == "delivered" and not delivered_at:
                            delivered_at = now
                        self.db.update_project_status(
                            name,
                            delivery_status=auto_status,
                            last_delivered_at=delivered_at or None)
                        logger.info("交付状态更新: %s %s -> %s",
                                    name, current_status, auto_status)
            except Exception as e:
                logger.error("后台交付状态检测失败: %s", e)
            finally:
                self._delivery_check_running = False

        threading.Thread(target=_check, daemon=True).start()

    def clear_cache(self):
        with self._lock:
            self._output_dir_cache.clear()

    # ==================== 项目自定义状态 ====================

    def _move_to_completed(self, project_name):
        """将组内NAS上的项目目录移动到 O盘/00已完成/ 子目录。
        用于设为"已完成"时归档项目。
        """
        proj = self.db.get_project(project_name)
        if not proj:
            return False, "项目不存在"

        group_root = self.nas.get("group_root", "")
        group_path = proj.get("group_path", "")
        if not group_path or not os.path.isdir(group_path):
            # 组内路径不存在（可能本来就只有制作部NAS侧的记录），跳过
            return True, "项目无组内NAS目录，跳过移动"

        target_root = os.path.join(group_root, "00已完成")
        project_dir_name = os.path.basename(group_path.rstrip("\\/"))
        target_path = os.path.join(target_root, project_dir_name)

        # 已经在 00已完成 下了？跳过
        norm_target_root = os.path.normpath(target_root).rstrip("\\/")
        norm_group_path = os.path.normpath(group_path).rstrip("\\/")
        if norm_group_path.startswith(norm_target_root + os.sep) or norm_group_path == norm_target_root:
            return True, "项目已在 00已完成 下，跳过移动"

        # 目标已存在同名目录（用户可能已手动移动过）
        if os.path.isdir(target_path):
            return True, "00已完成 下已存在同名目录，跳过移动"

        # 确保 00已完成 目录存在
        try:
            os.makedirs(target_root, exist_ok=True)
        except (PermissionError, OSError):
            unc_target_root = self._to_unc(target_root)
            result = subprocess.run(
                ["cmd", "/c", "mkdir", unc_target_root],
                capture_output=True, timeout=30)
            if result.returncode != 0 and not os.path.isdir(target_root):
                return False, "无法创建 00已完成 目录"

        # 执行移动
        moved = False
        try:
            shutil.move(group_path, target_path)
            moved = True
        except Exception as e:
            logger.warning("直接 shutil.move 失败，尝试 cmd move: %s", e)

        if not moved:
            src_unc = self._to_unc(group_path)
            dst_unc = self._to_unc(target_path)
            result = subprocess.run(
                ["cmd", "/c", "move", "/-y", src_unc, dst_unc],
                capture_output=True, timeout=300)
            if result.returncode != 0 or not os.path.isdir(target_path):
                err = (result.stderr or result.stdout or b"Unknown")
                err_text = err.decode("gbk", errors="replace").strip()
                return False, "移动失败: " + err_text

        # 更新 DB 中的 group_path
        self.db.update_project_status(project_name, group_path=target_path)
        # 清缓存
        with self._lock:
            self._output_dir_cache.clear()

        logger.info("项目已移入 00已完成: %s -> %s", group_path, target_path)
        self.db.add_sync_log(
            project_name, "移入已完成", "group",
            file_path=target_path, status="success",
            message="已移入 00已完成")
        return True, "项目已移入 00已完成"

    def set_custom_status(self, project_name, status):
        """设置项目的自定义状态（剪辑中/审核中/修改中/待交付/已完成）。
        - 设为"修改中"时，在01上映单集版目录中新建以日期命名的文件夹（如0810修改）。
        - 设为"待交付"时，自动将交付文件夹模板复制到项目根目录。
        - 设为"已完成"时，自动将组内NAS项目移动到 00已完成 子目录。
        """
        valid_statuses = ["", "剪辑中", "审核中", "修改中", "待交付", "已完成"]
        if status not in valid_statuses:
            return False, "无效的状态: " + str(status)

        proj = self.db.get_project(project_name)
        if not proj:
            return False, "项目不存在"

        old_status = proj.get("custom_status", "")
        self.db.update_project_status(project_name, custom_status=status)
        logger.info("项目状态变更: %s %s -> %s", project_name, old_status, status)

        # 设为"修改中"时，在01上映单集版目录中新建日期文件夹
        if status == "修改中":
            ok, msg = self._create_revision_folder(project_name)
            if not ok:
                return True, "状态已更新为修改中，但创建修改文件夹失败: " + msg
            return True, "状态已更新为修改中，" + msg

        # 设为"待交付"时，自动复制交付文件夹到项目根目录
        if status == "待交付":
            ok, msg = self._copy_delivery_folder(project_name)
            if not ok:
                return True, "状态已更新为待交付，但交付文件夹复制失败: " + msg
            return True, "状态已更新为待交付，交付文件夹已复制到项目根目录"

        # 设为"已完成"时，自动将组内NAS项目移入 00已完成 子目录
        if status == "已完成":
            ok, msg = self._move_to_completed(project_name)
            if not ok:
                return True, "状态已更新为已完成，但移动项目失败: " + msg
            return True, "状态已更新为已完成，" + msg

        return True, "状态已更新为: " + status

    def _create_revision_folder(self, project_name):
        """在项目的01上映单集版目录中新建以日期命名的修改文件夹（如0810修改）。
        如果同名文件夹已存在则不重复创建。
        管理员进程无法直接写映射盘符，用 UNC 路径 + cmd mkdir 兜底。
        """
        proj = self.db.get_project(project_name)
        if not proj:
            return False, "项目不存在"

        group_path = proj.get("group_path", "")
        if not group_path:
            return False, "项目无组内路径"

        # 查找01上映单集版目录
        output_dirs = self._find_output_dirs(group_path, project_name)
        if not output_dirs:
            return False, "未找到%s目录" % self._get_output_dir_name(project_name)

        output_dir = output_dirs[0]

        # 生成日期文件夹名：MMDD修改
        now = datetime.now()
        folder_name = "%02d%02d修改" % (now.month, now.day)
        folder_path = os.path.join(output_dir, folder_name)

        # 检查是否已存在（用 UNC 路径检查，绕过权限隔离）
        unc_path = self._to_unc(folder_path)
        if os.path.isdir(unc_path) or os.path.isdir(folder_path):
            return True, "修改文件夹已存在: " + folder_name

        try:
            # 方案1：直接 Python makedirs（非管理员模式可用）
            try:
                os.makedirs(folder_path, exist_ok=True)
            except (PermissionError, OSError):
                # 方案2：管理员权限隔离，用 cmd mkdir + UNC 路径兜底
                result = subprocess.run(
                    ["cmd", "/c", "mkdir", unc_path],
                    capture_output=True, timeout=30)
                if result.returncode != 0 and not os.path.isdir(folder_path):
                    raise OSError(
                        "无法创建文件夹（权限被拒绝）。"
                        "请确保程序以非管理员模式运行（双击 start.bat 启动即可）。")

            logger.info("创建修改文件夹: %s", folder_path)
            self.db.add_sync_log(
                project_name, "创建修改文件夹", "group",
                file_path=folder_path, status="success",
                message="已创建: " + folder_name)
            return True, "已创建修改文件夹: " + folder_name
        except Exception as e:
            self.db.add_sync_log(
                project_name, "创建修改文件夹失败", "group",
                file_path=folder_path, status="error", message=str(e))
            return False, str(e)

    def _copy_delivery_folder(self, project_name):
        """将交付文件夹整个复制到项目根目录下（组内NAS侧）。
        结果结构：项目根目录/000交付/00成片/...
        不修改项目原有任何文件。
        """
        proj = self.db.get_project(project_name)
        if not proj:
            return False, "项目不存在"

        group_path = proj.get("group_path", "")
        if not group_path:
            return False, "项目无组内路径"

        src = self._delivery_folder
        if not os.path.isdir(src):
            return False, "交付文件夹不存在: " + src

        # 目标：项目根目录/000交付（保留文件夹名，整体复制进去）
        folder_name = os.path.basename(src.rstrip("\\/"))
        dst = os.path.join(group_path, folder_name)

        logger.info("复制交付文件夹: %s -> %s", src, dst)
        self.db.add_sync_log(
            project_name, "复制交付文件夹", "delivery_folder->group",
            file_path=src, status="info",
            message="源: " + src + " -> 目标: " + dst)

        try:
            # 注意：不能用 /MIR 镜像模式！这里用 /E 纯增量复制
            cmd = ["robocopy", src, dst] + ROBOCOPY_BASE
            dst_unc = self._to_unc(dst)
            unc_alt = None
            if dst_unc != dst:
                unc_alt = ["robocopy", src, dst_unc] + ROBOCOPY_BASE
            ok, msg, rc = _exec(cmd, TIMEOUT_XCOPY_BIG,
                                label="copy_delivery_folder", unc_alt=unc_alt)
            if ok:
                self.db.add_sync_log(
                    project_name, "交付文件夹复制完成", "delivery_folder->group",
                    file_path=src, status="success", message="已复制到: " + dst)
                return True, "复制完成"
            else:
                self.db.add_sync_log(
                    project_name, "交付文件夹复制失败", "delivery_folder->group",
                    file_path=src, status="error", message=msg)
                return False, "robocopy 返回码 %d: %s" % (rc, msg)
        except Exception as e:
            self.db.add_sync_log(
                project_name, "交付文件夹复制异常", "delivery_folder->group",
                file_path=src, status="error", message=str(e))
            return False, str(e)

    # ==================== 一键交付（组内 → 制作部） ====================

    def deliver_to_production(self, project_name):
        """一键交付：把组内NAS项目下的 000交付 整个复制到制作部NAS对应项目的 000交付 目录。
        异步执行，进度通过 get_deliver_status 查询。
        """
        proj = self.db.get_project(project_name)
        if not proj:
            return False, "项目不存在"

        group_path = proj.get("group_path", "") or ""
        prod_path = proj.get("production_path", "") or ""
        if not prod_path:
            return False, "该项目无制作部NAS路径，无法一键交付"

        folder_name = os.path.basename(self._delivery_folder.rstrip("\\/"))
        src = os.path.join(group_path, folder_name)
        if not os.path.isdir(src):
            return False, "组内NAS项目下不存在 %s 目录" % folder_name

        dst = os.path.join(prod_path, folder_name)

        with self._lock:
            existing = self._deliver_tasks.get(project_name)
            if existing and existing.get("status") in ("running", "starting"):
                return False, "已有交付任务在进行中"

        total_files = 0
        try:
            for _, _, files in os.walk(src):
                total_files += len(files)
        except OSError:
            pass

        run_id = self.db.insert_deliver_run(
            project_name, src, dst, total_files,
            status="running", message="正在准备...",
            started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        with self._lock:
            self._deliver_tasks[project_name] = {
                "run_id": run_id,
                "status": "starting",
                "total": total_files,
                "current": 0,
                "pct": 0,
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "message": "正在准备...",
                "src": src,
                "dst": dst,
            }

        self.db.add_sync_log(
            project_name, "一键交付启动", "group->production",
            file_path=src, status="info",
            message="目标: " + dst + "，共 " + str(total_files) + " 个文件")

        threading.Thread(
            target=self._run_deliver_to_production,
            args=(project_name, src, dst),
            daemon=True).start()
        threading.Thread(
            target=self._poll_deliver_progress,
            args=(project_name,),
            daemon=True).start()

        return True, "交付已启动，共 %d 个文件" % total_files

    # ==================== 初版交付（成片目录整体推送） ====================

    def deliver_initial_version(self, project_name):
        """把组内 NAS 的 01上映单集版 目录整体推送到制作部 NAS，完成后状态自动变为"审核中"。
        触发场景：剪辑中的项目点击"统计"后发现集数已达标。
        """
        proj = self.db.get_project(project_name)
        if not proj:
            return False, "项目不存在"

        if proj.get("custom_status") != "剪辑中":
            return False, "仅剪辑中的项目可执行初版交付"

        src, err = self.get_source_dir(project_name)
        if not src:
            return False, "组内成片目录未找到: " + str(err)

        dst, err = self.get_dest_dir(project_name)
        if not dst:
            # 制作部成片目录还不存在，自动创建
            prod_path = proj.get("production_path", "")
            dst = os.path.join(prod_path, self._get_output_dir_name(project_name))
            try:
                os.makedirs(dst, exist_ok=True)
            except Exception as e:
                return False, "无法创建制作部成片目录: " + str(e)

        with self._lock:
            existing = self._deliver_tasks.get(project_name)
            if existing and existing.get("status") in ("running", "starting"):
                return False, "已有交付任务在进行中"

        total_files = 0
        try:
            for _, _, files in os.walk(src):
                total_files += len(files)
        except OSError:
            pass

        run_id = self.db.insert_deliver_run(
            project_name, src, dst, total_files,
            status="running", message="初版交付 - 正在准备...",
            started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        with self._lock:
            self._deliver_tasks[project_name] = {
                "run_id": run_id,
                "task_type": "initial_version",
                "status": "starting",
                "total": total_files,
                "current": 0,
                "pct": 0,
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "message": "初版交付 - 正在准备...",
                "src": src,
                "dst": dst,
            }

        self.db.add_sync_log(
            project_name, "初版交付启动", "group->production",
            file_path=src, status="info",
            message="目标: " + dst + "，共 " + str(total_files) + " 个文件")

        threading.Thread(
            target=self._run_deliver_initial_version,
            args=(project_name, src, dst),
            daemon=True).start()
        threading.Thread(
            target=self._poll_deliver_progress,
            args=(project_name,),
            daemon=True).start()

        return True, "初版交付已启动，共 %d 个文件" % total_files

    def _run_deliver_initial_version(self, project_name, src, dst):
        with self._lock:
            task = self._deliver_tasks.get(project_name, {})
            task["status"] = "running"
            task["message"] = "初版交付 - 正在复制..."
        proc = None
        try:
            os.makedirs(dst, exist_ok=True)
            cmd = ["robocopy", src, dst] + ROBOCOPY_FAST
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            with self._lock:
                task["proc_pid"] = proc.pid
            stdout, stderr = proc.communicate(timeout=TIMEOUT_ROBOCOPY_FAST)
            rc = proc.returncode
            success = rc < 8
            if success:
                with self._lock:
                    task["current"] = task["total"]
                    task["pct"] = 100
                    task["status"] = "done"
                    task["message"] = "初版交付完成，状态→审核中"
                    task["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.db.update_project_status(
                    project_name,
                    custom_status="审核中",
                    delivery_status="delivered",
                    last_delivered_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
                self.db.add_sync_log(
                    project_name, "初版交付完成→审核中", "group->production",
                    file_path=src, status="success",
                    message="成片已推送到: " + dst + "，robocopy rc=" + str(rc))
            else:
                err = stderr.decode("gbk", errors="replace")[:500] \
                    if stderr else "robocopy rc=%d" % rc
                with self._lock:
                    task["status"] = "error"
                    task["message"] = "初版交付失败: " + err
                self._cleanup_partial_dst(dst)
                self.db.add_sync_log(
                    project_name, "初版交付失败", "group->production",
                    file_path=src, status="error", message=err)
        except subprocess.TimeoutExpired:
            if proc:
                proc.kill()
                proc.wait()
            with self._lock:
                task["status"] = "error"
                task["message"] = "初版交付超时（超过1小时）"
            self._cleanup_partial_dst(dst)
        except Exception as e:
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            with self._lock:
                task["status"] = "error"
                task["message"] = "初版交付异常: " + str(e)
        finally:
            run_id = task.get("run_id")
            if run_id:
                try:
                    with self._lock:
                        final_status = task.get("status", "unknown")
                        final_msg = task.get("message", "")
                    self.db.finish_deliver_run(
                        run_id, final_status, final_msg,
                        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                except Exception as _e:
                    logger.warning("finish_deliver_run 失败: %s", _e)

    def _poll_deliver_progress(self, project_name):
        """独立后台线程：周期性 os.walk 目标目录更新进度（不在 API 请求里做，避免卡死）"""
        import time as _time
        for _ in range(240):  # 最多 240*2=480s
            _time.sleep(2)
            with self._lock:
                task = self._deliver_tasks.get(project_name)
                if not task:
                    break
                if task.get("status") not in ("running", "starting"):
                    break
                dst = task.get("dst", "")
            if not dst:
                continue
            try:
                cur = 0
                for _, _, files in os.walk(dst):
                    cur += len(files)
                with self._lock:
                    task["current"] = cur
                    if task["total"] > 0:
                        task["pct"] = min(round(cur / task["total"] * 100), 99)
                    else:
                        task["pct"] = 0
            except OSError:
                pass

    def _run_deliver_to_production(self, project_name, src, dst):
        with self._lock:
            task = self._deliver_tasks.get(project_name, {})
            task["status"] = "running"
            task["message"] = "正在复制..."
        proc = None
        try:
            cmd = ["robocopy", src, dst] + ROBOCOPY_FAST
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            with self._lock:
                task["proc_pid"] = proc.pid
            stdout, stderr = proc.communicate(timeout=3600)
            rc = proc.returncode
            success = rc < 8
            if success:
                with self._lock:
                    task["current"] = task["total"]
                    task["pct"] = 100
                    task["status"] = "done"
                    task["message"] = "交付完成"
                    task["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.db.update_project_status(
                    project_name,
                    delivery_status="delivered",
                    last_delivered_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    custom_status="已完成",
                )
                self.db.add_sync_log(
                    project_name, "一键交付完成", "group->production",
                    file_path=src, status="success",
                    message="已交付到: " + dst + "，robocopy rc=" + str(rc))
            else:
                err = stderr.decode("gbk", errors="replace")[:500] \
                    if stderr else "robocopy rc=%d" % rc
                with self._lock:
                    task["status"] = "error"
                    task["message"] = "交付失败: " + err
                self._cleanup_partial_dst(dst)
                self.db.add_sync_log(
                    project_name, "一键交付失败", "group->production",
                    file_path=src, status="error", message=err)
        except subprocess.TimeoutExpired:
            if proc:
                proc.kill()
                proc.wait()
            with self._lock:
                task["status"] = "error"
                task["message"] = "交付超时（超过1小时）"
            self._cleanup_partial_dst(dst)
        except Exception as e:
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            with self._lock:
                task["status"] = "error"
                task["message"] = "交付异常: " + str(e)
        finally:
            run_id = task.get("run_id")
            if run_id:
                try:
                    with self._lock:
                        final_status = task.get("status", "unknown")
                        final_msg = task.get("message", "")
                    self.db.finish_deliver_run(
                        run_id, final_status, final_msg,
                        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                except Exception as _e:
                    logger.warning("finish_deliver_run 失败: %s", _e)

    def _cleanup_partial_dst(self, dst):
        """交付失败/超时后清理目标目录，避免留下半成品"""
        if not dst or not os.path.exists(dst):
            return
        try:
            import shutil as _shutil
            _shutil.rmtree(dst, ignore_errors=True)
            logger.warning("已清理交付失败的半成品目录: %s", dst)
        except Exception as e:
            logger.warning("清理半成品目录失败 %s: %s", dst, e)

    def shutdown(self, wait=False):
        """优雅停机：终止所有在跑的 robocopy 子进程"""
        pids = []
        with self._lock:
            for name, task in list(self._deliver_tasks.items()):
                if task.get("status") == "running" and task.get("proc_pid"):
                    pids.append((name, task["proc_pid"]))
        for name, pid in pids:
            try:
                import subprocess as _sp
                # Windows: taskkill /F /T /PID 终止整个进程树
                _sp.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True, timeout=5)
                logger.warning("已终止交付子进程 PID=%s (%s)", pid, name)
            except Exception as e:
                logger.warning("终止 PID=%s 失败: %s", pid, e)
        # 把所有 running 任务标为 aborted
        with self._lock:
            for name, task in self._deliver_tasks.items():
                if task.get("status") == "running":
                    task["status"] = "aborted"
                    task["message"] = "服务关闭时中止"
        logger.info("SyncEngine.shutdown() 完成，清理了 %d 个交付进程", len(pids))

    def get_deliver_status(self, project_name):
        """查询交付任务状态（纯内存读取，不做文件系统操作）"""
        with self._lock:
            task = self._deliver_tasks.get(project_name)
            if not task:
                return {"status": "idle"}
            resp = {k: v for k, v in task.items() if k not in ("src",)}
        return resp

    # ==================== 集数管理 ====================

    def set_episodes(self, project_name, total, current):
        """设置项目集数（总集数和当前已输出集数）。
        当 current >= total 且 total > 0 时，返回通知标志。
        """
        proj = self.db.get_project(project_name)
        if not proj:
            return False, "项目不存在", False
        try:
            total = int(total) if total else 0
            current = int(current) if current else 0
        except (ValueError, TypeError):
            return False, "集数必须为整数", False
        if total < 0 or current < 0:
            return False, "集数不能为负数", False
        if current > total and total > 0:
            current = total  # 不超过总集数

        self.db.update_project_status(
            project_name, total_episodes=total, current_episodes=current)
        logger.info("集数设置: %s 总%d集 当前%d集", project_name, total, current)

        completed = total > 0 and current >= total
        return True, "集数已更新", completed

    def auto_count_episodes(self, project_name):
        """自动统计01上映单集版目录中的成片文件数量作为当前集数"""
        proj = self.db.get_project(project_name)
        if not proj:
            return 0
        group_path = proj.get("group_path", "")
        if not group_path:
            return 0
        output_dirs = self._find_output_dirs(group_path, project_name)
        if not output_dirs:
            return 0
        count = 0
        video_exts = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.wmv', '.m4v'}
        for od in output_dirs:
            try:
                for name in os.listdir(od):
                    full = os.path.join(od, name)
                    if os.path.isfile(full):
                        ext = os.path.splitext(name)[1].lower()
                        if ext in video_exts:
                            count += 1
            except OSError:
                continue
        return count

    # ==================== 多模式文件列表 ====================

    def list_all_revision_folders(self, project_name):
        """列出项目01上映单集版目录中所有修改文件夹（MMDD修改格式）。
        返回 [{"name": "MMDD修改", "path": full_path}, ...] 按日期倒序排列。
        """
        proj = self.db.get_project(project_name)
        if not proj:
            return []
        group_path = proj.get("group_path", "")
        if not group_path:
            return []
        output_dirs = self._find_output_dirs(group_path, project_name)
        if not output_dirs:
            return []

        import re
        rev_pattern = re.compile(r'^(\d{4})(修改)?$')
        candidates = []
        for od in output_dirs:
            try:
                for name in os.listdir(od):
                    full = os.path.join(od, name)
                    if os.path.isdir(full):
                        m = rev_pattern.match(name)
                        if m:
                            candidates.append({"name": name, "path": full, "date": m.group(1)})
            except OSError:
                continue

        candidates.sort(key=lambda x: x["date"], reverse=True)
        return candidates

    def find_revision_folder(self, project_name):
        """查找项目01上映单集版目录中最近的修改文件夹（MMDD修改格式）。
        返回 (folder_path, folder_name) 或 (None, None)。
        """
        folders = self.list_all_revision_folders(project_name)
        if not folders:
            return None, None
        return folders[0]["path"], folders[0]["name"]

    def get_delivery_stats(self, project_name):
        """返回项目 000交付 子文件夹的统计信息，用于卡片首页渲染。
        Returns:
            {
                "found": bool,                # 000交付 目录是否存在
                "total_episodes": int,        # 项目总集数（来自 DB）
                "items": [
                    {"key": "成片", "label": "成片", "type": "episode", "current": 60, "total": 60},
                    {"key": "有音乐", "label": "有音乐无字幕版本", "type": "episode", "current": 60, "total": 60},
                    {"key": "无音乐", "label": "无音乐无bgm版本", "type": "episode", "current": 60, "total": 60},
                    {"key": "字幕", "label": "字幕文件", "type": "episode", "exclude": ["无字幕"], "current": 60, "total": 60},
                    {"key": "截图", "label": "工程截图", "type": "screenshot", "current": 5, "total": 5},
                ],
                "overall_pct": int,            # 综合完成百分比
            }
        """
        proj = self.db.get_project(project_name)
        group_path = ""
        if proj:
            group_path = proj.get("group_path", "") or ""

        if not group_path:
            return {"found": False, "total_episodes": 0, "items": [], "overall_pct": 0}

        folder_name = os.path.basename(self._delivery_folder.rstrip("\\/"))
        base = os.path.join(group_path, folder_name)
        total_episodes = (proj.get("total_episodes", 0) or 0) if proj else 0

        items = [
            {"key": "成片", "label": "成片", "type": "episode", "current": 0, "total": total_episodes},
            {"key": "有音乐", "label": "有音乐无字幕版本", "type": "episode", "current": 0, "total": total_episodes},
            {"key": "无音乐", "label": "无音乐无bgm版本", "type": "episode", "current": 0, "total": total_episodes},
            {"key": "字幕", "label": "字幕文件", "type": "episode", "exclude": ["无字幕"], "current": 0, "total": total_episodes},
            {"key": "截图", "label": "工程截图", "type": "screenshot", "current": 0, "total": 5},
        ]

        found_folders = set()
        if os.path.isdir(base):
            try:
                for name in os.listdir(base):
                    full = os.path.join(base, name)
                    if not os.path.isdir(full):
                        continue
                    low = name.lower()
                    matched_idx = -1
                    for idx, it in enumerate(items):
                        if it.get("exclude"):
                            skip = False
                            for ex in it["exclude"]:
                                if ex.lower() in low:
                                    skip = True
                                    break
                            if skip:
                                continue
                        if it["key"].lower() in low:
                            matched_idx = idx
                            break
                    if matched_idx >= 0:
                        found_folders.add(name)
                        cnt = 0
                        try:
                            for sub in os.listdir(full):
                                if os.path.isfile(os.path.join(full, sub)):
                                    cnt += 1
                        except OSError:
                            pass
                        items[matched_idx]["current"] = cnt
            except OSError:
                pass

        # 综合完成率（只算项目已设置了 total_episodes 的 episode 项 + 截图）
        checked_items = [it for it in items if it["type"] == "screenshot" or it["total"] > 0]
        total_cur = sum(it["current"] for it in checked_items)
        total_max = sum(it["total"] for it in checked_items)
        overall_pct = round(total_cur / total_max * 100) if total_max > 0 else 0

        return {
            "found": len(found_folders) > 0,
            "total_episodes": total_episodes,
            "items": items,
            "overall_pct": overall_pct,
        }

    def list_files_by_mode(self, project_name, mode="editing", subpath=""):
        """按模式列出文件。
        - editing: 01上映单集版根目录的文件（剪辑中）
        - revising: 01上映单集版/MMDD修改/ 目录的文件（修改中）
        - delivery: 000交付/[subpath] 目录的文件夹和文件（待交付/已完成）
        """
        proj = self.db.get_project(project_name)
        group_path = ""
        if proj:
            group_path = proj.get("group_path", "") or ""
        else:
            # 项目未在 DB 登记 —— 尝试从组内 NAS 反查路径
            group_root = self.config.get("nas", {}).get("group_root", "")
            for candidate in (
                os.path.join(group_root, "00已完成", project_name),
                os.path.join(group_root, project_name),
            ):
                if candidate and os.path.isdir(candidate):
                    group_path = candidate
                    break

        if not group_path:
            if mode == "delivery":
                return {"folders": [], "files": [], "breadcrumbs": []}
            return []

        if mode == "editing":
            return self.list_output_files(project_name)

        elif mode == "revising":
            result = {"folders": [], "files": [], "breadcrumbs": []}
            result["breadcrumbs"].append({"name": "修改文件夹", "path": ""})

            if not subpath:
                folders = self.list_all_revision_folders(project_name)
                for f in folders:
                    result["folders"].append({"name": f["name"], "path": f["name"]})
                return result

            # subpath = "MMDD修改" → 列出该文件夹内的文件
            rev_folder_name = subpath.replace("/", "\\").strip("\\")
            folders = self.list_all_revision_folders(project_name)
            rev_path = None
            rev_name = None
            for f in folders:
                if f["name"] == rev_folder_name:
                    rev_path = f["path"]
                    rev_name = f["name"]
                    break

            if rev_path and os.path.isdir(rev_path):
                result["breadcrumbs"].append({"name": rev_name, "path": rev_name})
                files = []
                try:
                    for name in os.listdir(rev_path):
                        full = os.path.join(rev_path, name)
                        if os.path.isfile(full):
                            ext = os.path.splitext(name)[1].lower()
                            size = os.path.getsize(full)
                            files.append({
                                "name": name,
                                "path": full,
                                "size": size,
                                "size_mb": round(size / 1024 / 1024, 1),
                                "ext": ext,
                                "mtime": datetime.fromtimestamp(
                                    os.path.getmtime(full)).strftime(
                                    "%Y-%m-%d %H:%M"),
                                "parent_dir": rev_name,
                            })
                except OSError:
                    pass
                files.sort(key=lambda x: _natural_key(x["name"]))
                result["files"] = files

            return result

        elif mode == "delivery":
            folder_name = os.path.basename(self._delivery_folder.rstrip("\\/"))
            base = os.path.join(group_path, folder_name)
            target = base
            if subpath:
                safe_sub = subpath.replace("/", "\\").strip("\\")
                target = os.path.normpath(os.path.join(base, safe_sub))
                if not target.startswith(os.path.normpath(base)):
                    target = base

            result = {
                "folders": [],
                "files": [],
                "breadcrumbs": [],
                "total_episodes": (proj.get("total_episodes", 0) or 0) if proj else 0,
                "screenshot_expected": 5,
                "project_name": project_name,
            }

            # 构建面包屑
            rel = os.path.relpath(target, group_path) if os.path.isdir(target) else folder_name
            parts = rel.replace("/", "\\").split("\\")
            bc_path = ""
            for i, part in enumerate(parts):
                if i == 0:
                    result["breadcrumbs"].append({"name": part, "path": ""})
                else:
                    bc_path = os.path.join(bc_path, part) if bc_path else part
                    result["breadcrumbs"].append({"name": part, "path": bc_path})

            if not os.path.isdir(target):
                return result

            try:
                for name in os.listdir(target):
                    full = os.path.join(target, name)
                    if os.path.isdir(full):
                        rel_to_base = os.path.relpath(full, base)
                        file_count = 0
                        try:
                            for sub_name in os.listdir(full):
                                if os.path.isfile(os.path.join(full, sub_name)):
                                    file_count += 1
                        except OSError:
                            pass
                        result["folders"].append({
                            "name": name,
                            "path": rel_to_base.replace("/", "\\"),
                            "file_count": file_count,
                        })
                    elif os.path.isfile(full):
                        ext = os.path.splitext(name)[1].lower()
                        size = os.path.getsize(full)
                        rel_to_base = os.path.relpath(full, base)
                        result["files"].append({
                            "name": name,
                            "path": full,
                            "rel_path": rel_to_base.replace("/", "\\"),
                            "size": size,
                            "size_mb": round(size / 1024 / 1024, 1),
                            "ext": ext,
                            "mtime": datetime.fromtimestamp(
                                os.path.getmtime(full)).strftime(
                                "%Y-%m-%d %H:%M"),
                        })
            except OSError:
                pass

            result["folders"].sort(key=lambda x: _natural_key(x["name"]))
            result["files"].sort(key=lambda x: _natural_key(x["name"]))
            return result

        return []

    def get_file_path_for_preview(self, project_name, filename, mode="editing", subpath=""):
        """根据模式获取文件的实际路径用于预览"""
        if mode == "editing":
            files = self.list_output_files(project_name)
            for f in files:
                if f["name"] == filename:
                    return f["path"]
        elif mode == "revising":
            rev_folder = subpath if subpath else ""
            data = self.list_files_by_mode(project_name, "revising", rev_folder)
            files = data["files"] if isinstance(data, dict) else data
            for f in files:
                if f["name"] == filename:
                    return f["path"]
        elif mode == "delivery":
            proj = self.db.get_project(project_name)
            if not proj:
                return None
            group_path = proj.get("group_path", "")
            folder_name = os.path.basename(self._delivery_folder.rstrip("\\/"))
            base = os.path.join(group_path, folder_name)
            if subpath:
                target = os.path.normpath(os.path.join(base, subpath.replace("/", "\\").strip("\\")))
            else:
                target = base
            full = os.path.join(target, filename)
            if os.path.isfile(full):
                return full
        return None

    # ==================== 修改中回传 ====================

    def deliver_revision_file(self, project_name, file_name, rev_folder_name=None):
        """将修改文件夹中的单个文件回传到制作部NAS的对应修改文件夹中。
        目标：制作部NAS/项目/01上映单集版/MMDD修改/
        rev_folder_name: 指定修改文件夹名（如 "0810修改"），为 None 时使用最新的。
        """
        proj = self.db.get_project(project_name)
        if not proj:
            return False, "项目不存在"

        if rev_folder_name:
            folders = self.list_all_revision_folders(project_name)
            rev_path = None
            rev_name = rev_folder_name
            for f in folders:
                if f["name"] == rev_folder_name:
                    rev_path = f["path"]
                    break
            if not rev_path:
                return False, "未找到修改文件夹: " + rev_folder_name
        else:
            rev_path, rev_name = self.find_revision_folder(project_name)
            if not rev_path:
                return False, "未找到修改文件夹"

        src = os.path.join(rev_path, file_name)
        if not os.path.isfile(src):
            for name in os.listdir(rev_path):
                if name == file_name and os.path.isfile(os.path.join(rev_path, name)):
                    src = os.path.join(rev_path, name)
                    break
            else:
                return False, "文件不存在于修改文件夹: " + file_name

        prod_path = proj.get("production_path", "")
        if not prod_path:
            return False, "该项目无制作部路径"

        prod_output_dirs = self._find_output_dirs(prod_path, project_name)
        if not prod_output_dirs:
            return False, "制作部项目中未找到%s目录" % self._get_output_dir_name(project_name)

        dst_dir = os.path.join(prod_output_dirs[0], rev_name)
        dst = os.path.join(dst_dir, file_name)

        try:
            file_size = os.path.getsize(src)

            # 创建目标修改文件夹（用 UNC 路径兜底）
            try:
                os.makedirs(dst_dir, exist_ok=True)
            except (PermissionError, OSError):
                dst_dir_unc = self._to_unc(dst_dir)
                result = subprocess.run(
                    ["cmd", "/c", "mkdir", dst_dir_unc],
                    capture_output=True, timeout=30)
                if result.returncode != 0 and not os.path.isdir(dst_dir):
                    raise OSError("无法创建修改文件夹: " + dst_dir)

            # 复制文件
            try:
                shutil.copy(src, dst)
            except (PermissionError, OSError):
                src_unc = self._to_unc(src)
                dst_unc = self._to_unc(dst)
                result = subprocess.run(
                    ["cmd", "/c", "copy", "/y", src_unc, dst_unc],
                    capture_output=True, timeout=120)
                if result.returncode != 0:
                    err = (result.stderr or result.stdout or b"Unknown error")
                    raise OSError(err.decode("gbk", errors="replace").strip())

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.db.add_delivery_log(
                project_name, file_name, src, dst, file_size,
                "success", "修改回传成功 -> " + rev_name)
            self.db.update_project_status(
                project_name, delivery_status="delivered",
                last_delivered_at=now)
            return True, "修改回传成功: " + dst
        except Exception as e:
            self.db.add_delivery_log(
                project_name, file_name, src, dst, 0,
                "error", str(e))
            return False, str(e)

    def deliver_revision_batch(self, project_name, file_names, rev_folder_name=None):
        """批量回传修改文件夹中的文件到制作部NAS的修改文件夹"""
        results = []
        total = len(file_names)
        success_count = 0
        fail_count = 0

        self.db.update_project_status(
            project_name,
            delivery_status="delivering",
            sync_progress="回传 0/{}".format(total))

        for i, fname in enumerate(file_names):
            ok, msg = self.deliver_revision_file(project_name, fname, rev_folder_name)
            if ok:
                success_count += 1
            else:
                fail_count += 1
            results.append({
                "name": fname,
                "ok": ok,
                "message": msg,
                "index": i + 1,
                "total": total,
            })
            self.db.update_project_status(
                project_name,
                delivery_status="delivering",
                sync_progress="回传 {}/{}".format(i + 1, total))

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if success_count > 0:
            self.db.update_project_status(
                project_name, delivery_status="delivered",
                sync_progress="", last_delivered_at=now)
        else:
            self.db.update_project_status(
                project_name, delivery_status="error",
                sync_progress="全部失败 0/{}".format(total),
                last_delivered_at="")
        return results


    def deliver_revision_folder(self, project_name, rev_folder_name):
        """将整个修改文件夹（含全部文件）回传到制作部NAS的对应修改文件夹。"""
        proj = self.db.get_project(project_name)
        if not proj:
            return False, "项目不存在"

        folders = self.list_all_revision_folders(project_name)
        rev_path = None
        for f in folders:
            if f["name"] == rev_folder_name:
                rev_path = f["path"]
                break
        if not rev_path or not os.path.isdir(rev_path):
            return False, "未找到修改文件夹: " + rev_folder_name

        prod_path = proj.get("production_path", "")
        if not prod_path:
            return False, "该项目无制作部路径"

        prod_output_dirs = self._find_output_dirs(prod_path, project_name)
        if not prod_output_dirs:
            return False, "制作部项目中未找到%s目录" % self._get_output_dir_name(project_name)

        dst_dir = os.path.join(prod_output_dirs[0], rev_folder_name)

        try:
            try:
                os.makedirs(dst_dir, exist_ok=True)
            except (PermissionError, OSError):
                dst_dir_unc = self._to_unc(dst_dir)
                result = subprocess.run(
                    ["cmd", "/c", "mkdir", dst_dir_unc],
                    capture_output=True, timeout=30)
                if result.returncode != 0 and not os.path.isdir(dst_dir):
                    raise OSError("无法创建目标文件夹: " + dst_dir)

            try:
                shutil.copytree(rev_path, dst_dir, dirs_exist_ok=True)
            except (PermissionError, OSError):
                src_unc = self._to_unc(rev_path)
                dst_unc = self._to_unc(dst_dir)
                result = subprocess.run(
                    ["cmd", "/c", "xcopy", "/E", "/I", "/Y", src_unc + "\\*", dst_unc + "\\"],
                    capture_output=True, timeout=600)
                if result.returncode != 0 and not os.path.isdir(dst_dir):
                    err = (result.stderr or result.stdout or b"Unknown error")
                    raise OSError(err.decode("gbk", errors="replace").strip())

            file_count = 0
            total_size = 0
            for root, _, files in os.walk(rev_path):
                for fn in files:
                    file_count += 1
                    try:
                        total_size += os.path.getsize(os.path.join(root, fn))
                    except OSError:
                        pass

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.db.add_delivery_log(
                project_name, rev_folder_name + "/(整个文件夹)",
                rev_path, dst_dir, total_size,
                "success", "文件夹修改回传成功 -> " + rev_folder_name + " (" + str(file_count) + " 个文件)")
            self.db.update_project_status(
                project_name, delivery_status="delivered",
                last_delivered_at=now)
            return True, "文件夹修改回传成功: " + dst_dir + " (" + str(file_count) + " 个文件)"
        except Exception as e:
            self.db.add_delivery_log(
                project_name, rev_folder_name + "/(整个文件夹)",
                rev_path, dst_dir, 0,
                "error", str(e))
            return False, str(e)

    def deliver_revision_folders_batch(self, project_name, folder_names):
        """批量回传多个修改文件夹到制作部NAS"""
        results = []
        total = len(folder_names)
        success_count = 0
        fail_count = 0

        self.db.update_project_status(
            project_name,
            delivery_status="delivering",
            sync_progress="回传文件夹 0/{}".format(total))

        for i, fname in enumerate(folder_names):
            ok, msg = self.deliver_revision_folder(project_name, fname)
            if ok:
                success_count += 1
            else:
                fail_count += 1
            results.append({
                "name": fname,
                "ok": ok,
                "message": msg,
                "index": i + 1,
                "total": total,
            })
            self.db.update_project_status(
                project_name,
                delivery_status="delivering",
                sync_progress="回传文件夹 {}/{}".format(i + 1, total))

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if success_count > 0:
            self.db.update_project_status(
                project_name, delivery_status="delivered",
                sync_progress="", last_delivered_at=now)
        else:
            self.db.update_project_status(
                project_name, delivery_status="error",
                sync_progress="全部失败 0/{}".format(total),
                last_delivered_at="")
        return results
