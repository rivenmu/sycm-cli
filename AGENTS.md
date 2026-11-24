# AGENTS.md

给接手这个仓库的 AI 助手看的约定。人读的说明在 `README.md`，命令细节在 `tools/sycm.md`。

## 这是什么

`shopdata` 工具集的代码仓库（版本见 `SKILL.md` 的 `version`）。通过本机 Chrome 的登录态读取淘宝/天猫店铺数据，全部只读。入口是 `scripts/tb.sh <工具> <命令>`，MCP 服务名 `shopdata`。

本仓库当前只装了生意参谋（sycm）一个模块，其余模块由 `modules_manifest.json` 决定是否下载。

## 目录结构

```
tb/
├── core/                 跨平台公共层：登录态、请求、风控词、护栏、日志、账号、模块、MCP
├── platforms/
│   └── sycm/             生意参谋
│       ├── cli.py        命令注册（build_parser）与大部分命令实现
│       ├── item.py       单品分析命令与 item-report 的取数
│       ├── datafetch.py  自助分析「取数报表」的列表与下载
│       ├── platform.py   平台声明：hosts、风控词、只读正则、登录 cookie
│       ├── session.py    业务检查、店铺识别
│       ├── fields.json   指标字段的中文名与口径
│       └── report/       单品诊断报告：取数、深度分析、渲染、Excel、跟踪
├── mcp_server.py         MCP server 入口
└── modules_manifest.json 模块清单
scripts/                  各平台启动脚本（.sh / .cmd）与 tb.sh
tools/sycm.md             给 AI 读的生意参谋完整命令手册
references/               分析方法（日检、周复盘、单品诊断）
extension/                取数桥浏览器插件（Windows 必需，Mac 可不装）
```

## 运行

```bash
./scripts/tb.sh sycm doctor                 # 先确认登录态
./scripts/tb.sh sycm --help                 # 命令总览
./scripts/tb.sh sycm datafetch list         # 例：列出取数报表
```

- 依赖由 `scripts/tb.sh` 用 uv 按需注入（browser-cookie3、curl-cffi、openpyxl 等），不要另外装到系统 Python。
- Mac 直接读 Chrome 登录；Windows 必须装 `extension/` 里的插件。取数方式由 `tb/core/transport.py` 决定。
- 没有测试套件。改完至少跑 `doctor` 和目标命令各一次，用真实返回验证。
- 运行时日志在 `tb log`（本机 jsonl，保留 14 天），排错先看它。

## 编码克制

- 只处理真会发生的失败（登录失效、风控、接口返回异常、文件损坏、串店），不为「理论上可能」加兜底分支与静默重试；错误按现有退出码暴露，不吞。风控、验证码、登录失效三类**不重试**的红线不变。
- 不新建测试套件、不追覆盖率；改完跑 `doctor` 与目标命令，用真实返回验证。
- 能改一处解决的不重构模块；不为将来可能的需求加参数、开关与插件点。总则见 [platform/AGENTS.md](../platform/AGENTS.md)「工程与产出规范」。

## 加一个命令的步骤

1. 命令实现放在 `tb/platforms/sycm/` 下的模块里（顶层模块或 `report/` 子包均可），不要写进 `tb/core`。
2. 所有请求走 `_api_get` / `client.get`，不要直接 `requests.get`。这样才拿得到登录判定、风控词、请求间隔和运行日志。
3. 在 `cli.py` 的 `build_parser()` 里注册子命令；新模块用延迟导入避免循环依赖。
4. 同步文档：`tools/sycm.md`（命令手册）、`README.md` 功能表、`功能清单.md`。
5. 只读边界：路径里出现 `add/create/update/delete/save/...` 会被 `platform.py` 的 `write_re` 拒绝，新增接口先确认是不是读操作。

## 红线

- 不提交登录资料、cookie、店铺数据、买家信息；输出目录已在 `.gitignore` 里。
- 不代填账号密码、不处理验证码、不绕过平台风控。遇到登录失效让用户在浏览器里重新登录。
- 风控、验证码、登录失效三类错误不重试。
- 改命令行为前先跟用户确认；结构性重构（挪文件、拆模块）单独确认后再动。

## 退出码

`0` 成功（含跳过）｜`1` 一般失败｜`2` 登录失效｜`3` 触发风控（见 `tb/core/errors.py`）。

## 已知约束

- 生意参谋「取数报表」分个人空间和公共空间，接口默认返回公共空间，读个人空间必须带 `space=personal`。
- 报表是滚动日期窗口，下载文件的日期区间由报表自身配置决定，命令不传日期。
- `cookie2` 是会话 cookie，Chrome 关掉就没有登录态，定时任务必须保证 Chrome 开着并已登录。
- 取数报表的 `lyone` 回包不带店铺编号，串店保护不适用于这条链路。

## 升级与本地改动

本仓库用 git 管理，`origin` 是 `github.com/rakei076/sycm-cli`，主干 `main`。本地的 `datafetch` 是自加功能，升级时注意两类覆盖：

1. **`git pull`**：只看冲突。上游若改了 `cli.py`、`tools/sycm.md`，我们那几行注册和文档段落可能要手工合并；`tb/platforms/sycm/datafetch.py` 是独立文件，一般不受影响。
2. **模块包重装（`modules enable` / `modules install`）**：会按白名单直接覆盖文件，不经过 git。白名单是 `tb/platforms/<模块>/`、`tools/<模块>.md`、`scripts/<模块>.sh|.cmd`、`references/`。`sycm` 模块包重装会覆盖 `cli.py`（丢掉 `datafetch` 注册）和 `tools/sycm.md`（丢掉文档），但不会删 `datafetch.py`——文件还在，命令却不在，属于静默失效。`AGENTS.md`、`README.md`、`功能清单.md` 不在白名单，不受影响。

升级步骤：先提交或备份本地改动，再执行更新；更新后立刻验证 `tb sycm --help | grep datafetch` 和 `tb sycm datafetch list`，发现注册丢了就补回 `build_parser()` 里的三行。
