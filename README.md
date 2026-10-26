# SuperStar · 学习通自动刷课（Windows 桌面版）

超星学习通 Windows 桌面客户端。`api/` 负责超星协议的网络与解析，`engine.py` 负责学习编排。

> ⚠️ 本项目仅供学习交流，请勿用于商业用途；使用产生的后果由使用者自行承担。

---

## 功能

- **进度以服务端为准**：章节是否完成一律看平台返回的「已完成」，本地缓存只做辅助记录；
  平台要求重新作答（判分不合格 / 未提交）的章节会被自动清除本地完成标记并重新处理。
- **按账号 uid 缓存进度**：`progress_cache/<uid>.json`，格式 `{courseId: {pointId: true}}`，多账号互不干扰。
- **账号自动登录**：勾选「记住账号与密码」后写入 `progress_cache/accounts.json`，
  密码用 Windows DPAPI 以当前用户身份加密（换机器/换用户无法解密）；下次启动自动登录。
  取消勾选则只记住手机号，登录框自动回填。
- **账号切换**：下拉选择已登录账号（显示手机号 + uid），切换时同步切换进度缓存。
- **读取进度**：一键调用服务端接口，用真实完成状态**双向对齐**本地缓存（完成则标记、未完成则清除）；
  同时逐章探测章节是否开放，未开放的章节在列表里显示「未开放」而不是「待学」。
  整个过程异步进行：先出完成状态，再逐行点亮「未开放」，界面不卡；期间按钮禁用，切课程/开始刷课会中断探测。
- **答题不合格自动重答**：章节检测提交后回读任务点，平台仍判未通过则重新作答，
  次数由 `config_win.ini` 的 `[tiku] work_retry` 控制（默认 1，0 表示不重答）；
  重答后仍不合格的章节保留「未完成」，不计入已完成。
- **未开放章节智能处理**：先回退检查上一章是否未完成并重做，再回到当前章验证是否解锁；
  上一章已完成则每 15 秒轮询当前章，直到开放或用户停止。
- **演示模式（仅源码/测试，不随发行包）**：内置假后端，无需账号/网络即可跑通全流程。

---

## AI 使用情况

**产品内的 AI 能力（可选，默认停用）**

| 环节 | 说明 |
| --- | --- |
| 题库提供方 | `[tiku] provider` 可选 `TikuYanxi`（言溪）、`TikuLike`（LIKE 知识库）、`TikuAdapter`（自建）、`AI`（任意 OpenAI 兼容端点）、`DeepSeek`（官方）、`SiliconFlow`（硅基流动）；界面下拉切换后只显示该提供方需要填的项 |
| 用途 | 仅用于回答章节检测 / 作业的选择、填空、判断、简答题；视频、文档、阅读等非作答任务点不走 AI |
| 调用方式 | 题目 + 选项拼成 prompt，要求模型以 `{"Answer": [...]}` JSON 输出，按题型校验后才写入答案缓存 |
| 开关与降级 | 未填 API Key 即停用题库，跳过答题；`openai` / `httpx` 为惰性导入的可选依赖，缺失只影响大模型类提供方，其余题库与刷课照常 |
| 数据范围 | 请求只含题目文本，不含账号、Cookie、学校等身份信息；题目与答案会落到 `progress_cache/cache.json` 本地缓存 |
| 频率控制 | `min_interval_seconds` 限制请求间隔，避免触发限流 |

**开发过程中的 AI 参与**

- 界面、引擎、构建脚本与文档在 AI 编程助手辅助下编写与迭代（代码经人工审阅、自检与冒烟测试后合入）。
- 本仓库不包含任何 AI 生成但未经验证的逻辑：所有功能均通过 `test/` 下的单测与冒烟测试，
  界面行为由 `python run.py --selftest` 断言。

---

## 目录结构

```
SuperStar_Windows/
├── api/                    # 超星核心（登录/章节/视频/文档/作业/直播/题库）
├── resource/               # font_map_table.json（加密题目字体映射）
├── engine.py               # 学习编排：进度缓存、未开放章节处理、事件回调
├── account_store.py        # 账号持久化（DPAPI 加密密码，启动自动登录）
├── paths.py                # 运行时路径：所有可写文件固定在程序目录内
├── config_store.py         # 配置双层管理：默认配置 + 用户配置（防覆盖 / 升级校验）
├── gui/
│   ├── app.py              # tkinter/ttk 界面（无第三方 GUI 依赖）
│   └── selftest.py         # 界面自检：真实驱动演示流程并校验控件
├── demo_chaoxing.py        # 演示用假后端（仅源码存在，发行包构建时排除）
├── run.py                  # 启动入口（--demo / --selftest 仅源码态可用）
├── version.txt             # 版本号唯一来源（X.Y.Z）：exe/zip 名与 Release tag 均由它推导
├── build.py                # 构建 exe + 铺装目录 + 分发 zip
├── config_win.ini          # 默认配置（随包自带，升级时覆盖 —— 用户不要改）
├── 使用说明.txt             # 随包分发的用户文档
├── LICENSE                 # GPL-3.0
└── test/                   # 引擎单测、端到端冒烟、GUI 冒烟、路径约定
```

---

## 运行（开发态）

要求 Python ≥ 3.10 且自带 tkinter（Windows 官方安装包默认包含）。

```bash
python -m pip install -r requirements_win.txt
python run.py              # 正常启动
python run.py --demo       # 演示模式（源码态）
```

cookies 登录：把 `cookies.txt`（格式 `k=v;k2=v2`）放到 `progress_cache/` 目录下。

---

## 分发形态：整个文件夹，不是单个 exe

运行期需要的 6 个条目**全部随压缩包分发**：

| 条目 | 作用 |
| --- | --- |
| `SuperStar-<版本>.exe` | 主程序 |
| `config_win.ini` | 默认配置（随包自带，升级时被覆盖，用户不要改） |
| `config_user.ini` | 用户配置（首次启动自动生成，升级不覆盖） |
| `chaoxing.log` | 运行日志（空文件，追加写入） |
| `progress_cache/` | 进度缓存 / 账号 / cookies / 题库答案缓存 |
| `使用说明.txt` | 随包说明 |
| `LICENSE` | GPL-3.0 许可文本（二进制分发必须随附） |

设计约束：

- 运行期所有写入都收进上述位置（`paths.py` 统一解析），exe 目录顶层只多出 `config_user.ini`；
- `config_win.ini` 不内嵌进 exe，只由压缩包随附；
- 压缩包内**不套顶层目录**（解压即得主目录），zip 名即默认解压文件夹名；
- 程序本身不做文件校验、不弹窗、不阻断启动，注意事项写在 `使用说明.txt`。

---

## 使用说明

1. 点「添加账号」登录（或勾选 cookies 登录）；
2. 左侧点击课程名勾选要刷的课程，可「全选」；
3. 设置倍速/并发，点「开始刷课」；右侧看章节状态，底部看日志，状态栏显示当前章节进度；
4. 「读取进度」用服务端真实进度刷新缓存；「刷新列表/刷新章节」重新拉取课程与章节；
5. 需要自动答题：设置 →「答题设置」里在下拉中选答案提供方式（言溪 / LIKE / TikuAdapter / AI / DeepSeek / SiliconFlow），
   下方只会显示该方式需要的项（TOKEN、服务地址，或接口地址 + API Key + 模型）；
   大模型三种方式各存各的配置，切换时会带出各自的默认地址与模型，只需补 API Key。也可手改 `config_user.ini`。

---

## 配置：默认配置 + 用户配置（防覆盖）

升级时用户会把新压缩包解压覆盖到旧目录，因此配置拆成两份：

| 文件 | 来源 | 升级时 | 用户是否可改 |
| --- | --- | --- | --- |
| `config_win.ini` | 随包分发 | **被覆盖** | 不要改（改了也会丢） |
| `config_user.ini` | 首次启动按默认配置生成 | **不覆盖** | 改这里 |

规则（`config_store.py` 实现）：

- **读取**：默认配置打底，用户配置逐键覆盖；用户显式留空也算覆盖（可用于停用题库）；
- **迁移**：每次启动用最新的默认配置校验用户配置
  - 新增键 → 以默认值补进用户配置；
  - 已删除的键 → 从原段移除，值保留到 `[obsolete]` 段（不生效、不丢数据）；
  - 取值非法（如 `cover_rate=abc`、`submit=yes`）→ 回落默认值，日志里写明原值；
- **写入**：只写用户配置，且走文本级最小改动（保留用户注释），迁移幂等。

---

## 构建与分发

**版本号唯一来源：仓库根目录 `version.txt`（X.Y.Z）**。exe 名、zip 名、Release tag（`v<版本>`）
发新版 = 改 `version.txt` → 打成 tag 推上去（`v<版本>`），Release 由 CI 产出。

```bash
python -m pip install pyinstaller
python build.py              # dist/ 铺成安装目录 + dist/SuperStar-<版本>-win64.zip
python build.py --clean      # 清理后重建
python build.py --exe-only   # 只出 exe
```

`python build.py` 做两件事：

1. **铺装 `dist/`**：exe + 随包 4 项 —— `dist/` 即用户解压后看到的文件夹，可直接双击验证；
2. **打 zip**：同等内容（含 `LICENSE`）直接放在 zip 根，解压即得主目录。

产物**不含演示模式**：构建时 `--exclude-module demo_chaoxing gui.selftest`，
`gui.app.demo_available()` 导入失败即隐藏「演示」按钮，`--demo` / `--selftest` 以退出码 3 拒绝。

```bash
python run.py --selftest   # 源码态界面自检，退出码 0 = 通过
python run.py --demo       # 源码态演示模式
dist/SuperStar-<版本>.exe --selftest   # 退出码 3：证明发行包已无演示能力
```

---

## 测试

```bash
python test/run_test.py           # 一键跑全部（推荐）
python test/run_test.py --dist    # 额外校验 dist 下发行 exe 拒绝 --selftest（退出码 3）

python test/test_engine.py        # 引擎逻辑单测（mock，无网络）
python test/test_demo_smoke.py    # 引擎端到端冒烟（假后端，无网络）
python test/test_gui_smoke.py     # 界面冒烟：真实 Tk 窗口跑演示流程并断言控件
python test/test_paths.py         # 路径约定：随包文件清单 + zip 内容
```

`test_gui_smoke.py` 校验：课程/章节渲染、章节状态全部更新为「完成」、日志非空、
开始按钮恢复可用、布局未被窗口裁切。

> 测试依赖仓库内的 `demo_chaoxing.py`，只能在源码目录运行，**也不在 CI 中执行**
> （CI 只做构建与发行包基本校验，保持构建快速）。

---

## 发布 Release（GitHub Actions）

`.github/workflows/release.yml` 两条触发路径：

| 触发 | 条件 |
| --- | --- |
| 发版 | 推 tag `v<版本>`（`git tag v$(cat version.txt) && git push origin v$(cat version.txt)`） |
| 试构建 | Actions → Release → Run workflow（只出 Artifact，不发 Release） |

Tag 需与 `version.txt` 一致；带后缀（如 `v1.0.0-rc.1`）自动标为**预发布**。

流程（两个 job）：

1. **Build Windows executable**（windows-latest）：装依赖 → 校验 tag 与 `version.txt` →
   `python build.py` → 确认 exe / zip 产出 → `upload-artifact` 上传 zip；
2. **Publish GitHub Release**（ubuntu-latest）：取 tag 版本号 → 下载 Artifact →
   用「上一个 tag 至今的提交」生成 changelog → 创建 Release 并上传 zip（不单独上传裸 exe）。

> CI 不跑测试套件：`test_gui_smoke` 要建真实窗口，在 runner 上慢且易受环境影响，
> 该部分只在本地 `python test/run_test.py` 执行；
> zip 内容由 `build.py` 铺装逻辑保证，也不在 CI 里逐条校验。

---

## 已知限制

- 真实账号登录与刷课依赖超星服务端，需在自有 Windows 环境实跑确认；仓库内验证基于演示后端。
- 题库默认停用；`provider=AI` / `DeepSeek` / `SiliconFlow` 需自备 API Key，答案准确性由模型决定，不保证判分通过。
- `--selftest` 只验证界面与引擎接线，不替代真实刷课验证。

---

## 开源协议

本项目采用 **GNU General Public License v3.0（GPL-3.0）**，完整文本见 [LICENSE](LICENSE)。

选择 GPL-3.0 的理由：本项目是学习/研究性质的社区工具，copyleft 能保证任何衍生版本与二次分发
继续以同等自由度开放，避免被闭源包装成商业产品。

据此你需要遵守：

- 分发或修改本项目（含二进制分发）时，必须一并提供完整源代码与 GPL-3.0 许可；
- 保留原作者署名与许可声明，并标注修改；
- 不得附加任何限制下游用户权利的条款；
- 本项目按「原样」提供，不含任何担保与责任。

**使用与免责**（非许可条款，属使用者责任提示）：

- 仅供学习交流，请勿用于商业用途，并遵守所在学校的规定与平台服务条款；
- 使用本工具产生的账号风险、学业后果由使用者自行承担，与作者无关；
- 本项目与超星（北京世纪超星信息技术发展有限责任公司）无任何关联，
  所有商标与产品名称归各自所有者。

---

## 致谢

- 刷课核心 `api/` 源自 [SuperStar](https://github.com/menghuanshiguang/SuperStar_R_Android) 项目，
  本仓库在其基础上做 Windows 桌面化与工程封装。
- `CacheDAO` 参考 [xuexiaoyi-to-xuexitong-tampermonkey-proxy](https://github.com/SocialSisterYi/xuexiaoyi-to-xuexitong-tampermonkey-proxy)。
- 第三方题库服务：言溪题库、LIKE 知识库、[TikuAdapter](https://github.com/DokiDoki1103/tikuAdapter)。
