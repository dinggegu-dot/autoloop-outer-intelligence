import unittest

from superagent.goal_checks import normalize
from superagent.goal_team_plan import normalize_packages, prerequisites, ready_packages


def stage(*names):
    checks = [{"id": name, "script": f"tools/{name}.py", "args": [],
               "files": {"fixtures/input.txt": "synthetic"}, "expected": None}
              for name in names]
    return normalize({"version": 1, "stages": [{"id": "stage", "checks": checks}]})["stages"][0]


def package(name, *dependencies, **overrides):
    value = {"id": name, "capability": "implement", "objective": f"计算 {name}",
             "depends_on": list(dependencies), "outputs": [f"tools/{name}.py"], "checks": [name]}
    value.update(overrides)
    return value


class GoalTeamPlanTests(unittest.TestCase):
    def test_serial_and_single_package(self):
        contract = stage("a")
        self.assertEqual(normalize_packages([], contract), [])
        plan = normalize_packages([package("a", capability="analyze")], contract)
        self.assertEqual(plan, [package("a", capability="analyze")])
        self.assertEqual(prerequisites(plan, "a"), [])

    def test_stable_topological_order_and_readiness(self):
        contract = stage("a", "b", "c", "z")
        raw = [package("z"), package("c", "b", "a"), package("b", "a"), package("a")]
        plan = normalize_packages(raw, contract)
        self.assertEqual([item["id"] for item in plan], ["a", "b", "c", "z"])
        self.assertEqual(normalize_packages(raw[::-1], contract), plan)
        self.assertEqual([item["id"] for item in ready_packages(plan, (), ())], ["a", "z"])
        self.assertEqual([item["id"] for item in ready_packages(plan, ["a"], ["z"])], ["b"])
        self.assertEqual(prerequisites(plan, "c"), ["a", "b"])
        self.assertEqual(prerequisites(plan, "z"), [])
        with self.assertRaisesRegex(ValueError, "未声明"):
            prerequisites(plan, "missing")

    def test_reject_invalid_schema_capability_and_limits(self):
        for raw in (
            None, {}, [package("a")] * 7, [package("a", extra=True)],
            [package("a", capability=[])], [package("a", objective=" ")],
            [package("a", outputs=[])], [package("a", checks=[])],
            [package("a", outputs=[f"path-{i}" for i in range(13)])],
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_packages(raw, stage("a"))

    def test_reject_unknown_self_and_circular_dependencies(self):
        for raw, error in (
            ([package("a", "missing"), package("b")], "未声明的依赖"),
            ([package("a", "a"), package("b")], "依赖自身"),
            ([package("a", "b"), package("b", "a")], "循环"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                normalize_packages(raw, stage("a", "b"))

    def test_requires_exact_check_coverage_and_owned_script(self):
        with self.assertRaisesRegex(ValueError, "未覆盖"):
            normalize_packages([package("a")], stage("a", "b"))
        with self.assertRaisesRegex(ValueError, "未声明的阶段检查"):
            normalize_packages([package("a", checks=["missing"])], stage("a"))
        with self.assertRaisesRegex(ValueError, "重复覆盖"):
            normalize_packages([package("a"), package("b", checks=["a"])], stage("a", "b"))
        with self.assertRaisesRegex(ValueError, "脚本声明为本包输出"):
            normalize_packages([package("a", outputs=["tools/A.py"])], stage("a"))
        plan = normalize_packages([package("a", outputs=["tools/b.py", "tools/a.py"],
                                           checks=["b", "a"])], stage("a", "b"))
        self.assertEqual(plan[0]["checks"], ["a", "b"])

    def test_reject_unsafe_paths_and_overlaps(self):
        for path in ("../escape.py", "/root.py", "a//z", "a\\z", "a/.git/x",
                     "a/.hidden", "a/AGENTS.md", "中文.txt", "tools/A.py",
                     "tools/a.py/item", "tools", "FIXTURES", "fixtures/INPUT.txt",
                     "fixtures/input.txt/sub"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                normalize_packages([package("a", outputs=["tools/a.py", path])], stage("a"))
        with self.assertRaisesRegex(ValueError, "输出路径重叠"):
            normalize_packages([
                package("a", outputs=["tools/a.py", "reports/result"]),
                package("b", outputs=["tools/b.py", "REPORTS/RESULT/part.txt"]),
            ], stage("a", "b"))
        plan = normalize_packages(
            [package("a", outputs=["tools/a.py", "fixtures/output.txt", "tools/a.py.bak"])], stage("a")
        )
        self.assertEqual(len(plan[0]["outputs"]), 3)


if __name__ == "__main__":
    unittest.main()
