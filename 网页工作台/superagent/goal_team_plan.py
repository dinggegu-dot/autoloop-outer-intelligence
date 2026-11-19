"""目标阶段的工作包校验与依赖调度。"""
import re


_FIELDS = {"id", "capability", "objective", "depends_on", "outputs", "checks"}
_CAPABILITIES = {"implement", "analyze"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_COMPONENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}")


def _id_list(value, label, minimum, maximum):
    if (type(value) is not list or not minimum <= len(value) <= maximum
            or any(type(item) is not str or not _ID.fullmatch(item) for item in value)):
        raise ValueError(f"{label}须包含 {minimum} 至 {maximum} 个有效 ID")
    if len(set(value)) != len(value):
        raise ValueError(f"{label}包含重复 ID")
    return sorted(value)


def _output_path(value, package_id):
    if type(value) is not str or len(value) > 240:
        raise ValueError(f"工作包 {package_id} 的输出路径无效")
    parts = value.split("/")
    if len(parts) > 8 or any(
        not _COMPONENT.fullmatch(part) or part.lower() in {".git", "agents.md"}
        for part in parts
    ):
        raise ValueError(f"工作包 {package_id} 的输出须为安全、非隐藏的 ASCII 相对文件路径")
    return value


def _overlaps(left, right):
    left, right = left.lower(), right.lower()
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _topological(packages):
    ordered = []
    remaining = {package["id"]: package for package in packages}
    completed = set()
    while remaining:
        ready = ready_packages(remaining.values(), completed, ())
        if not ready:
            raise ValueError("工作包依赖存在循环")
        package = min(ready, key=lambda item: item["id"])
        ordered.append(package)
        completed.add(package["id"])
        del remaining[package["id"]]
    return ordered


def normalize_packages(raw, stage) -> list:
    """校验规范阶段的工作包，返回独立的稳定拓扑列表。空列表表示串行。"""
    if type(raw) is not list or len(raw) > 6:
        raise ValueError("每个阶段的工作包须为列表，最多 6 个")
    if not raw:
        return []

    scripts = {check["id"]: check["script"] for check in stage["checks"]}
    fixtures = {path for check in stage["checks"] for path in check["files"]}
    packages = []
    package_ids, covered = set(), set()
    claimed_outputs = []
    for item in raw:
        if type(item) is not dict or item.keys() != _FIELDS:
            raise ValueError("工作包必须且只能包含 id、capability、objective、depends_on、outputs、checks")
        package_id = item["id"]
        if type(package_id) is not str or not _ID.fullmatch(package_id):
            raise ValueError("工作包 ID 须为 1 至 64 位 ASCII 字母、数字、下划线或连字符，首位须为字母或数字")
        if package_id in package_ids:
            raise ValueError(f"工作包 ID 重复：{package_id}")
        package_ids.add(package_id)
        capability, objective = item["capability"], item["objective"]
        if type(capability) is not str or capability not in _CAPABILITIES:
            raise ValueError(f"工作包 {package_id} 的能力须为 implement 或 analyze")
        if type(objective) is not str or not objective.strip() or "\0" in objective:
            raise ValueError(f"工作包 {package_id} 须提供非空目标")
        deps = _id_list(item["depends_on"], f"工作包 {package_id} 的依赖", 0, 5)
        if package_id in deps:
            raise ValueError(f"工作包 {package_id} 不能依赖自身")

        outputs = item["outputs"]
        if type(outputs) is not list or not 1 <= len(outputs) <= 12:
            raise ValueError(f"工作包 {package_id} 须声明 1 至 12 个输出")
        paths = []
        for value in outputs:
            path = _output_path(value, package_id)
            if any(_overlaps(path, fixture) for fixture in fixtures):
                raise ValueError(f"工作包 {package_id} 的输出与验收夹具重叠：{path}")
            if any(_overlaps(path, prior) for prior in claimed_outputs):
                raise ValueError(f"工作包 {package_id} 的输出路径重叠：{path}")
            claimed_outputs.append(path)
            paths.append(path)

        checks = _id_list(item["checks"], f"工作包 {package_id} 的检查", 1, len(scripts))
        for check_id in checks:
            if check_id not in scripts:
                raise ValueError(f"工作包 {package_id} 引用了未声明的阶段检查：{check_id}")
            if check_id in covered:
                raise ValueError(f"阶段检查被多个工作包重复覆盖：{check_id}")
            if scripts[check_id] not in paths:
                raise ValueError(f"工作包 {package_id} 须将检查 {check_id} 的脚本声明为本包输出")
            covered.add(check_id)
        packages.append({"id": package_id, "capability": capability, "objective": objective.strip(),
                         "depends_on": deps, "outputs": sorted(paths), "checks": checks})

    missing = scripts.keys() - covered
    if missing:
        raise ValueError("工作包未覆盖阶段检查：" + "、".join(sorted(missing)))
    for package in packages:
        unknown = set(package["depends_on"]) - package_ids
        if unknown:
            raise ValueError(f"工作包 {package['id']} 含未声明的依赖：" + "、".join(sorted(unknown)))
    return _topological(packages)


def ready_packages(packages, completed_ids, active_ids) -> list:
    """按传入的规范顺序返回尚未启动、且直接依赖均已完成的工作包。"""
    completed = set(completed_ids)
    unavailable = completed | set(active_ids)
    return [package for package in packages if package["id"] not in unavailable
            and completed.issuperset(package["depends_on"])]


def prerequisites(packages, package_id) -> list:
    """按规范顺序返回指定工作包的全部传递前置包 ID。"""
    by_id = {package["id"]: package for package in packages}
    if package_id not in by_id:
        raise ValueError("未声明指定的工作包")
    seen = set()
    pending = list(by_id[package_id]["depends_on"])
    while pending:
        dep = pending.pop()
        if dep not in seen:
            seen.add(dep)
            pending.extend(by_id[dep]["depends_on"])
    return [item for item in by_id if item in seen]
