# 归档区（不参与自动测试）

这里放**已完成使命、不再每次执行**的东西。`test/run_test.py` 只扫 `test/test_*.py`，
不会进本目录。

## probes/ —— 一次性平台探针（v1.0.0-rc5 采集题库开发期）

调试「作业提交 / 成绩页 / 答案字段」时，用来对着真实超星账号逐页探测结构的临时脚本：

| 脚本 | 当时要解决的问题 |
| --- | --- |
| `_probe_list.py` / `_probe_list2.py` | 找「作业列表」入口，拿已批阅的成绩页 |
| `_probe_work.py` | 作业详情页结构（题目 / 选项 / 提交入口） |
| `_probe_scan.py` | 批量扫描课程任务点，定位可读答案的作业 |
| `_probe_c2.py` | 第二门课的作业页，找一份「已批阅」的成绩页 |
| `_probe_submit.py` / `_probe_submit2.py` | 提交作业后的判分页解析（对错 / 得分 / 正确答案） |
| `_probe_view.py` | 「查看答案」入口是否开放 |

它们的探测结论已经固化进 `api/work_verify.py` 与 `api/local_bank.py`，
并有断言式回归测试覆盖（`test/test_local_bank.py`），所以脚本本身不再需要每次运行。

> 注：这些探索最初是为了「从已完成账号批量采集答案」。真机验证发现平台对**已通过**的
> 章节测验会收回作答页（带作业号直连 403，去掉只给空白重做页），入口已试穷，
> 该功能（`api/collect.py` 与主界面「采集题库」）已删除，只保留刷课提交时自动入库。

**运行前需自备环境变量**（脚本已改为不硬编码账号）：

```bash
export CX_USER=<手机号>
export CX_PWD=<密码>
python test/archive/probes/_probe_list.py
```

`probes/_out/` 是当时的抓取产物（真实页面 HTML/JSON，含账号数据），
已在 `.gitignore` 里排除，仅本机保留供对照，不入库、不分发。
