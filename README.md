# SuperStar · 学习通自动刷课（Windows 桌面版）

超星学习通 Windows 桌面客户端。绿色解压即用，无需浏览器、无需 Python 环境。

> ⚠️ 本项目仅供学习交流，请勿用于商业用途；使用产生的后果由使用者自行承担。

---

## 为什么用它与同类方案对比

| 对比项 | 常见同类方案 | SuperStar Windows |
| --- | --- | --- |
| 运行方式 | 油猴/浏览器插件，须一直开着标签页 | 桌面客户端，纯 HTTP 直连，全程不需浏览器 |
| 浏览器自动化 | 部分依赖 Selenium/Playwright，需配 Chrome 与驱动 | 无浏览器自动化，无 WebDriver/驱动版本问题 |
| 环境要求 | 需自备 Python+依赖，或装浏览器扩展 | 解压即用，不需 Python，不装任何浏览器组件 |
| 登录态 | 依赖网页端登录，换浏览器/清 Cookie 即失效 | 客户端登录（密码 DPAPI 加密）或 cookies，与浏览器无关 |
| 界面 | 控制台/网页脚本，参数改配置文件 | 原生 GUI（tkinter/ttk），课程单选、倍速并发、章节状态、日志一屏可见 |
| 体积 | Electron 壳动辄上百 MB | 单 exe 约 28MB，常驻内存约 70MB |
| 多账号 | 基本不支持 | 多账号保存切换，进度按 uid 隔离 |

## 功能要点

- **不依赖浏览器**：网络层全走 `requests` 直连超星接口（登录/章节/视频/文档/作业/直播/题库），前端改版不影响使用。
- **进度以服务端为准**：章节完成看平台返回，本地缓存仅辅助；平台要求重做则清除本地标记重处理。
- **按账号 uid 缓存**：`progress_cache/<uid>.json`，格式 `{courseId:{pointId:true}}`。
- **账号自动登录/切换**：勾选「记住账号与密码」写入 `accounts.json`（密码 DPAPI 加密，换机/换用户不可解）；启动自动登录，下拉切换并同步进度缓存。
- **账号本机名称**：`accounts.json` 里每账号存 `name`（下拉显示「名称 · uid」）。名称留空时默认只用 uid，不允许与其他账号重名；「添加账号」不预填当前账号账密、同一 uid 不能重复添加；账号下拉右侧「账号设置」可改名或删除账号（仅删本机记录与登录态）。
- **读取进度**：一键用服务端真实状态双向对齐本地缓存，异步不卡界面。
- **答题不合格自动重答**：`[tiku] work_retry`（默认 3，0=不重答）；仍不合格保留未完成。
- **未开放章节智能处理**：先回退重做上一章，再验证当前章是否解锁；上一章不合格则无限重答（每 5 秒一轮），上一章已完成则每 15 秒轮询，直到开放或用户停止。
- **「进行中」标记**：当前章节/课程标「进行中」（仅会话前端效果，不落盘）。
- **视频观看进度**：播到 `[common] video_finish_percent`（90~100，默认 90）开始复查，不认可则继续看 5 秒再上报，循环至判通过或看完；取 100 为旧行为。设置→视频学习可改，即时生效。低于 90→90、高于 100→100、非法→90。
- **视频状态日志**：每 `[common] video_log_interval`（默认 10，0=关）秒输出进度行，刷课时可改即时生效。
- **界面主题**：浅色/深色/跟随系统（设置→外观，或 `[ui] theme`），跟随系统轮询注册表 `AppsUseLightTheme`。
- **本地题库**：只存正确答案，按课程共享（`progress_cache/local_bank/<课程ID>.json`），可停用、导出/导入/合并。详见下方「本地题库」。
- **批量采集**：主界面「采集题库」只读历史成绩页导答案，换账号直接命中。
- **演示模式**（仅源码/测试）：内置假后端免账号跑全流程。

---

## 快速使用

1. 解压发行包，双击 `SuperStar.exe`；
2. 点「添加账号」登录（或勾选 cookies 登录）；
3. 左侧点课程名选中要刷的课（单选，刷课中不能切课，需先「停止」）；
4. 设倍速/并发章节（默认 2.0 倍速 / 1 并发章节），点「开始刷课」；右侧看章节状态、底部看日志。
   取值写入 `config_user.ini` 的 `[common] speed/jobs`（1.0~2.0 倍速 / 1~8 并发），超出按边界纠正；
   「并发章节」= 同门课同时刷几章，单章任务点并发在设置→视频学习（默认 1）；遇未开放自动降串行。
5. 「读取进度」刷缓存；「刷新列表/刷新章节」重拉课程与章节；
6. 自动答题：设置→「答案提供方式」选（言溪/LIKE/TikuAdapter/AI/DeepSeek/SiliconFlow），下方只显该方式所需项；大模型各存各配置，切换带出默认地址与模型，补 API Key 即可。也可手改 `config_user.ini`。
7. 视频进度日志：设置→「视频日志」填间隔秒数（默认 10，0=不输出）。

---

## 分发形态：整个文件夹，不是单个 exe

运行期条目全部随压缩包分发：

| 条目 | 作用 |
| --- | --- |
| `SuperStar.exe` | 主程序（不带版本号） |
| `runtime/` | 运行时文件（Python/依赖/资源，exe 启动必需，勿删） |
| `config_win.ini` | 默认配置（随包，升级覆盖，勿改） |
| `config_user.ini` | 用户配置（首启生成，升级不覆盖） |
| `chaoxing.log` | 运行日志（首启创建，追加写入） |
| `progress_cache/` | 进度/账号/cookies/题库缓存（首启创建） |
| `使用说明.txt` | 随包说明 |
| `LICENSE` | GPL-3.0 |

设计约束：
- **PyInstaller onedir**（非 onefile）：免去每次解压上千文件到 `%TEMP%` 与首轮全量杀毒扫描。`runtime/` 须与 exe 平铺同级，否则报 `Failed to load Python DLL`。
- 所有写入收进上述位置，顶层只多出 `config_user.ini`；`config_win.ini` 不内嵌，只随包。
- 压缩包内不套顶层目录；升级建议先删旧文件夹再解压新包。

---

## AI 使用情况

**产品内 AI 能力（可选，默认停用）**

| 环节 | 说明 |
| --- | --- |
| 题库提供方 | `[tiku] provider`：`TikuYanxi`/`TikuLike`/`TikuAdapter`/`AI`/`DeepSeek`/`SiliconFlow`，界面下拉切后只显所需项 |
| 用途 | 仅回答章节检测/作业的选择、填空、判断、简答；视频/文档/阅读不走 AI |
| 调用 | 题+选项拼 prompt，要求 `{"Answer":[...]}` JSON，按题型校验后才写缓存 |
| 开关与降级 | 未填 API Key 即停用；`openai`/`httpx` 惰性导入，缺失只影响大模型类 |
| 数据范围 | 仅题目文本，不含账号/Cookie/学校；题目答案落 `cache.json` |
| 频率 | `min_interval_seconds` 限请求间隔，避免限流 |

**开发过程 AI 参与**：界面/引擎/构建/文档在 AI 助手辅助下编写迭代（经人工审阅与测试后合入）；所有功能由 `test/` 单测与 `python run.py --selftest` 断言。

---

## 配置：默认 + 用户（防覆盖）

| 文件 | 来源 | 升级时 | 用户可改 |
| --- | --- | --- | --- |
| `config_win.ini` | 随包 | **被覆盖** | 不要改 |
| `config_user.ini` | 首启按默认生成 | **不覆盖** | 改这里 |

规则（`config_store.py`）：
- 读取：默认打底，用户逐键覆盖（留空也算覆盖，可停用题库）；
- 迁移：新键补默认；已删键转 `[obsolete]`（不生效不丢数据）；非法值回落默认并写日志；
- 写入：只写用户配置，文本级最小改动，迁移幂等。

---

## 本地题库：只存正确答案，按课程共享

同一门课题目答案对所有账号相同，故**按课程存放、全账号共用**，换账号直接命中，不重复采集/耗额度。

工作方式：
1. 作业真正提交成功后重拉作答页，解析每题对错与「正确答案」；
2. 判对→收本次答案；判错但有「正确答案」→收正确答案；
3. 判错且拿不到答案/未批阅→不收（绝不猜）；
4. 只同步缺的题；已有的直接跳过（连核验都不发）；
5. 搜题最先查本地题库，命中即用，未命中才走题库/大模型；
6. 指纹=归一化标题+题型+归一化选项（去题号/分值/标签，选项排序），换账号/换章节/跨作业都能命中；
7. 入库存选项文本，选项乱序按文本重映射；文字对不上宁可不答。

存储：`progress_cache/local_bank/<课程ID>.json`（如 `高等数学_246831735.json`）。查询顺序：当前课程→其它课程。
旧结构启动时自动迁移：`local_bank.json` 与 `<账号>/<课程>.json` 合并进课程文件，旧目录改名 `.migrated` 保留；上一版 `<标题>_<ID>.json` 自动改名 `<ID>.json`（已存在则合并）。

| 操作 | 位置 / 命令 |
| --- | --- |
| 存储 | `progress_cache/local_bank/<课程ID>.json`（`[tiku] local_bank_dir` 可改根） |
| 界面 | 设置→「本地题库…」：统计/导出/导入合并/打开目录/清空 |
| 采集 | 主界面「采集题库」或 `python -m api.collect --course 关键词` |
| 命令行 | `python -m api.local_bank stats/list/export out.json/import in.json/merge a.json b.json -o 目录` |

导出 JSON 按课程分文件，导入即合并：同题答案相同累加确认次数，不同保留确认次数更多者。可多人共用。

开关（`config_user.ini` 的 `[tiku]`，改完重启或保存设置生效）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `local_bank` | `true` | 总开关；`false` 完全不读不写（文件保留） |
| `local_bank_verify` | `true` | 提交后核验补录；`false`=只读不写 |
| `local_bank_dir` | 空 | 题库根目录，留空=默认 |

> 判分页改版认不出时跳过收录（日志写明）；`local_bank_trust` 已移除，升级自动移入 `[obsolete]`。

### 批量采集（不用重做作业）

已刷完/高年级账号的作业页即成绩页。主界面「采集题库」（或 `python -m api.collect`）读一遍存题库，**只发 GET，不提交不存草稿**。

| 页面情况 | 收录 |
| --- | --- |
| 有「正确答案」 | 正确答案（判错也收） |
| 判对未公布答案 | 「我的答案」 |
| 判错无答案 | 跳过 |
| 未提交 | 跳过（记「未提交」） |
| 已提交未批阅/未公布 | 跳过（记「未批阅」） |
| 页面改版认不出 | 跳过（记「无法识别」，原页存 `collect_debug/`） |
| 库里已有且一致 | 跳过 |
| 库里已有但不一致 | 覆写纠正 |

用法：界面勾课→「采集题库」→确认→后台跑（日志逐章进度，`停止`可中断，已采保留）；
命令行 `python -m api.collect`（全部）/ `--course 高等数学,247100002` / `--max-works 3` / `--uid <uid>`（复用本机已存凭据，需先界面登录）。
采集走刷课同套限速器；落课程文件，换账号直接命中。

---

## 目录结构

```
SuperStar_Windows/
├── api/                    # 超星核心（登录/章节/视频/文档/作业/直播/题库/本地题库/判分/采集）
├── resource/               # font_map_table.json（加密题目字体映射）
├── engine.py               # 学习编排：进度缓存、未开放处理、事件回调
├── account_store.py        # 账号持久化（DPAPI 加密，自动登录）
├── paths.py                # 运行时路径统一解析
├── config_store.py         # 配置双层管理
├── gui/
│   ├── app.py              # tkinter/ttk 界面
│   └── selftest.py         # 界面自检
├── demo_chaoxing.py        # 演示假后端（仅源码，构建排除）
├── run.py                  # 启动入口（--demo/--selftest 仅源码态）
├── version.txt             # 版本号唯一来源
├── build.py                # 构建 exe + 铺装 + zip
├── config_win.ini          # 默认配置（升级覆盖）
├── 使用说明.txt             # 随包用户文档
├── LICENSE                 # GPL-3.0
└── test/                   # 分层测试（core/gui/slow）
    └── archive/            # 一次性探针脚本
```

---

## 运行（开发态）

Python ≥ 3.10 且自带 tkinter（Windows 官方包默认含）。

```bash
python -m pip install -r requirements_win.txt
python run.py              # 正常启动
python run.py --demo       # 演示（源码态）
```

cookies 登录：把 `cookies.txt`（`k=v;k2=v2`）放 `progress_cache/`。

---

## 构建与分发

**版本号唯一来源：`version.txt`**（zip/Release 全取它内容；exe 恒为 `SuperStar.exe`）。发新版=改 `version.txt`→打 tag `v<版本>`→推上去，Release 由 CI 产。

```bash
python -m pip install pyinstaller
python build.py              # dist/ 铺装 + SuperStar-<版本>-win64.zip
python build.py --clean      # 清理重建
python build.py --exe-only   # 只出 onedir 目录，不铺装不打 zip
```

`python build.py`：① 铺装 `dist/`（exe+runtime+随包文件，可直接双击验证）；② 打 zip（含 LICENSE，根即主目录）。

产物不含演示：`--exclude-module demo_chaoxing gui.selftest`，`gui.app.demo_available()` 导入失败即隐藏演示按钮，`--demo/--selftest` 退出码 3 拒绝。

```bash
python run.py --selftest                       # 源码态自检，退出码 0
dist/SuperStar/SuperStar.exe --selftest        # 退出码 3：证明发行包无演示
```

---

## 测试

按耗时与依赖分三层，**默认只跑 core**：

| 层 | 内容 | 命令 | 参考耗时 |
| --- | --- | --- | --- |
| `core` | 纯逻辑单测，无窗口/网络 | `python test/run_test.py` | ~45s |
| `gui` | 真实 Tk 窗口冒烟 | `python test/run_test.py --gui` | ~30s |
| `slow` | 日志文案/构建产物/版本约定 | `python test/run_test.py --slow` | ~15s |

```bash
python test/run_test.py              # core（默认）
python test/run_test.py --all        # core+gui+slow
python test/run_test.py --all --dist # 追加校验发行 exe 拒 --selftest（退出码 3）
python test/run_test.py --list       # 看分层清单
```

分层靠测试文件首行 `# layer: core|gui|slow` 标记，**新增测试缺省进 core**。`gui` 层无 tkinter 时回落 `.venv-build`，都没有则 SKIP。`test_gui_smoke.py` 校验渲染/状态更新/日志/按钮恢复/布局未裁切。一次性脚本归档 `test/archive/`，不参与调度，也不在 CI 跑（CI 只构建与发行校验）。

---

## 发布 Release（GitHub Actions）

`.github/workflows/release.yml` 两触发：推 tag `v<版本>`（与 `version.txt` 一致）发版；Actions→Release→Run workflow 试构建（只出 Artifact）。

两个 job：① Build（windows-latest）：装依赖→读 `version.txt`→`python build.py`→上传 zip；② Publish（ubuntu-latest）：取 tag→下 Artifact→用上次 tag 至今提交生成 changelog→建 Release 上传 zip（不单独传裸 exe）。
CI 不做版本/格式校验、不跑测试（`test_gui_smoke` 需真窗口，本地跑），zip 内容由 `build.py` 保证。

---

## 已知限制

- 真实登录/刷课依赖超星服务端，需自有 Windows 实跑确认；仓库验证基于演示后端。
- 题库默认停用；`AI/DeepSeek/SiliconFlow` 需自备 API Key，答案准确性由模型决定，不保证通过。
- `--selftest` 只验证界面与引擎接线，不替代真实刷课验证。
- 本地题库自动收录依赖「提交后能读出对错」：未批阅/判分页改版时不收录（只影响入库，不影响答题）；判错但页面给正确答案会收录纠错。
- 采集只对**已提交**作业有效：未交过的只有作答区，先刷课或自交一遍再采；教师关闭查看答案/未批阅时读不到答案，记「未批阅」跳过；页面改版认不出存 `collect_debug/`。

---

## 开源协议

GNU GPL-3.0（见 [LICENSE](LICENSE)）。copyleft 保证衍生与二次分发继续同等自由开放。

分发/修改（含二进制）须提供完整源码与许可、保留署名与修改标记、不附加限制下游的条款；按「原样」提供，无担保。

**使用与免责**：仅供学习交流，勿商用，遵守校规与平台条款；账号风险/学业后果由使用者自负；本项目与超星无任何关联，商标归各自所有者。

---

## 致谢

- 刷课核心 `api/` 源自 [SuperStar](https://github.com/menghuanshiguang/SuperStar_R_Android)。
- `CacheDAO` 参考 [xuexiaoyi-to-xuexitong-tampermonkey-proxy](https://github.com/SocialSisterYi/xuexiaoyi-to-xuexitong-tampermonkey-proxy)。
- 第三方题库：言溪题库、LIKE 知识库、[TikuAdapter](https://github.com/DokiDoki1103/tikuAdapter)。
