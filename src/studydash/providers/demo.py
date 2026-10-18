"""离线演示适配器：不联网，直接吐一份多平台样例数据。

用途有两个：
1. 第一次跑 ``studydash demo`` 就能看到完整看板长什么样，不用先配账号；
2. 前端模板、聚合层、CI 的回归测试都拿它当固定输入。

它不是假数据生成器——这份数据是手写的，覆盖了各种边界：已逾期、今天到期、
无截止时间、已评分、多个平台、进度条缺失、课程无作业。
"""

from __future__ import annotations

from datetime import timedelta

from ..model import Course, Progress, Assignment, now
from .base import Provider, ProviderMeta, register


def _at(days: float, hour: int = 23, minute: int = 59):
    """相对当前时间的天偏移 -> 带时区的 datetime。

    用相对时间而不是写死日期，这样演示数据永远不会「全过期」。
    """
    base = now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return base + timedelta(days=days)


def _a(
    cid: str,
    aid: str,
    title: str,
    course: str,
    platform: str,
    kind: str,
    *,
    status: str = "pending",
    due=None,
    score=None,
    max_score=None,
    url: str = "",
    detail: str = "",
) -> Assignment:
    return Assignment(
        id=aid,
        title=title,
        platform=platform,
        course_id=cid,
        course=course,
        kind=kind,
        status=status,
        due=due,
        score=score,
        max_score=max_score,
        url=url,
        detail=detail,
        platform_name={"chaoxing": "超星学习通", "icourse163": "中国大学 MOOC",
                       "manual": "手动导入"}.get(platform, platform),
    )


@register
class DemoProvider(Provider):
    meta = ProviderMeta(
        id="demo",
        name="离线演示",
        short="演示",
        status="stable",
        color="#0ea5e9",
        auth="none",
        note="内置样例数据，不联网。用于先看看效果。",
    )

    def fetch(self) -> list[Course]:
        courses: list[Course] = []

        # ---------------------------------------------------------------- #
        # 超星学习通 · 数据结构与算法（有一项已逾期）
        # ---------------------------------------------------------------- #
        dsa_id = "123456"
        courses.append(
            Course(
                id=dsa_id,
                name="数据结构与算法",
                platform="chaoxing",
                teacher="张明",
                term="2025-2026-2",
                url="https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu?courseid=123456",
                last_active=_at(-2, 10, 22),
                progress=[
                    Progress("任务点", 34, 51),
                    Progress("作业", 4, 6),
                ],
                assignments=[
                    _a(dsa_id, "9002", "实验二 链表实现", "数据结构与算法", "chaoxing",
                       "homework", due=_at(-1), detail="第 2 章",
                       url="https://mooc1.chaoxing.com/work/doHomeWorkNew?courseId=123456"),
                    _a(dsa_id, "9001", "第 3 章 单元测验", "数据结构与算法", "chaoxing",
                       "quiz", due=_at(1), max_score=100, detail="第 3 章",
                       url="https://mooc1.chaoxing.com/mooc-ans/mycourse/studentexam"),
                    _a(dsa_id, "9003", "第 4 章 视频任务点", "数据结构与算法", "chaoxing",
                       "video", due=_at(2), detail="第 4 章",
                       url="https://mooc1.chaoxing.com/mycourse/studentstudy"),
                    _a(dsa_id, "9000", "第 1 章 单元测验", "数据结构与算法", "chaoxing",
                       "quiz", status="graded", due=_at(-13), score=92, max_score=100,
                       detail="第 1 章"),
                ],
            )
        )

        # ---------------------------------------------------------------- #
        # 超星 · 操作系统（只有一条远期作业）
        # ---------------------------------------------------------------- #
        os_id = "654321"
        courses.append(
            Course(
                id=os_id,
                name="操作系统",
                platform="chaoxing",
                teacher="李红",
                term="2025-2026-2",
                url="https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu?courseid=654321",
                last_active=_at(-3, 19, 40),
                progress=[Progress("任务点", 18, 45), Progress("作业", 2, 4)],
                assignments=[
                    _a(os_id, "8001", "期末大作业", "操作系统", "chaoxing", "homework",
                       due=_at(47), max_score=100),
                ],
            )
        )

        # ---------------------------------------------------------------- #
        # 超星 · 大学英语（进度条齐全但没有截止时间）
        # ---------------------------------------------------------------- #
        en_id = "777777"
        courses.append(
            Course(
                id=en_id,
                name="大学英语（四）",
                platform="chaoxing",
                teacher="Wang Lin",
                term="2025-2026-2",
                last_active=_at(0, 8, 5),
                progress=[Progress("任务点", 57, 60)],
                assignments=[
                    _a(en_id, "7001", "Unit 8 听力任务", "大学英语（四）", "chaoxing",
                       "video", detail="Unit 8"),
                ],
            )
        )

        # ---------------------------------------------------------------- #
        # 中国大学 MOOC · 计算机网络（另一平台，也有逾期）
        # ---------------------------------------------------------------- #
        net_id = "83001"
        courses.append(
            Course(
                id=net_id,
                name="计算机网络",
                platform="icourse163",
                teacher="XJTU 教学组",
                term="2026 春",
                url="https://www.icourse163.org/learn/XJTU-83001",
                last_active=_at(-1, 22, 10),
                progress=[Progress("课时", 12, 24), Progress("测验", 3, 7)],
                assignments=[
                    _a(net_id, "77210", "第二周 单元作业", "计算机网络", "icourse163",
                       "homework", due=_at(-1, 23, 59), max_score=10, detail="第 2 周"),
                    _a(net_id, "77211", "第三周 单元测验", "计算机网络", "icourse163",
                       "quiz", due=_at(3), max_score=10, detail="第 3 周"),
                    _a(net_id, "77100", "第一周 单元测验", "计算机网络", "icourse163",
                       "quiz", status="graded", due=_at(-8), score=9, max_score=10,
                       detail="第 1 周"),
                ],
            )
        )

        # ---------------------------------------------------------------- #
        # 中国大学 MOOC · Python（全部完成，用来验证「没有待办」的样子）
        # ---------------------------------------------------------------- #
        py_id = "83002"
        courses.append(
            Course(
                id=py_id,
                name="Python 程序设计",
                platform="icourse163",
                teacher="王恺",
                term="2026 春",
                url="https://www.icourse163.org/learn/BIT-83002",
                last_active=_at(-4, 14, 30),
                progress=[Progress("课时", 33, 50)],
                assignments=[],
            )
        )

        # ---------------------------------------------------------------- #
        # 手动导入 · 线下课（演示「平台不提供接口也能进看板」）
        # ---------------------------------------------------------------- #
        mc_id = "morning-class"
        courses.append(
            Course(
                id=mc_id,
                name="形势与政策（线下）",
                platform="manual",
                teacher="陈老师",
                term="2025-2026-2",
                progress=[Progress("出勤", 4, 9)],
                assignments=[
                    _a(mc_id, "100", "课程论文提纲", "形势与政策（线下）", "manual",
                       "homework", due=_at(6, 18, 0), max_score=100, detail="课堂通知"),
                ],
            )
        )

        self.log.debug(f"演示数据：{len(courses)} 门课")
        return courses
