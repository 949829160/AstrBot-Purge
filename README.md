# AstrBot-Purge

> 升级杀不死的核心补丁引擎 —— 让每一处核心改动都可重复、可验证、可回滚。

AstrBot 的插件体系很灵活，但它改写不了 `backend/app` 里的核心逻辑：系统提示词怎么拼、工具审批怎么走、输出怎么后处理、内容过滤在哪一层生效。手动改核心代码能用，代价是每次升级都清零 —— 升级一次，改过的文件全被覆盖，而且没有任何记录告诉你「上次到底改了哪几行」。

AstrBot-Purge 把这件事变成一个可管理的工程问题：**改动被登记成补丁，补丁有版本、有指纹、有快照、有回滚点；升级之后自动重放，对不上就明确报错，而不是悄悄失效。**

---

## 它解决什么问题

| 没有它 | 有它 |
| --- | --- |
| 升级后核心改动全部丢失，只能凭记忆重改 | 启动时按版本自动重放补丁，22 个文件逐字节恢复 |
| 改坏了不知道改了哪几处、怎么退 | 每次写入前自动备份，一条命令回滚 |
| 补丁写死在文档里，和代码实际状态对不上 | 补丁表是唯一真相，`status` 直接告诉你每条是 OK 还是 GONE |
| 上游版本一变，补丁锚点错位，改到一半失败 | 版本闸门：版本不匹配时一个文件都不写 |
| 进程里跑的还是旧代码，磁盘上却是新代码 | 进程级生效检查：比对模块加载时间与磁盘指纹 |

---

## 核心能力

1. **锚点补丁表**：58 条在册补丁，每条带 `id / title / file / old / new`，只替换第一处，保留原文件 BOM 与行尾。
2. **整文件快照**：给 22 个被改写的核心文件做逐字节快照（dst/base 双指纹），重装或升级后整份写回；快照随包发布，首装即可到位。
3. **版本闸门**：快照带版本号；宿主版本变化时拒绝盲写，改为「先试重放，再重建快照」。
4. **跨版本迁移**：新上游里没动过的文件自动沿用；动过的文件单独列出，交给你决定重放还是放弃。
5. **启动自愈**：插件加载时问一次引擎，不健康就自动 `apply`，并把结果写进日志。
6. **进程级生效检查**：扫描 `sys.modules` 里已加载的核心模块，比对磁盘指纹与进程启动时间，告诉你「磁盘已改、进程未生效」。
7. **分级回滚**：`revert` 回到补丁前，`restore` 回到最近一次快照，两者都会先存一份现状备份。
8. **账本**：`tree-build / apply / revert / upgrade` 每次动作追加一条记录（最多 60 条），带版本、模式、计数。
9. **注入帧分段**：帧支持 `<!-- section:名字 -->` 分段，按当前会话状态挑段拼装；核心层每轮追加的那份会被先剥掉，避免同一段规则堆三层。
10. **输出清洗**：剥掉写作计划、工具脚手架和被代码块包住的正文；只拦「通篇就是拒答」的输出，正常短回复不动。
11. **命令面板**：`/purge` 一个入口覆盖状态、开关、重放、回滚、体检、快照。

---

## 工作方式

### 两层结构

```
核心层（backend/app）      插件层（data/plugins）
  58 条锚点补丁              注入帧拼装
  22 个文件快照              输出清洗
  版本闸门 / 账本            命令与体检
        └──────── repatch.py 是唯一真相 ────────┘
```

插件不维护自己的补丁清单。它读引擎的 `status --json`，所以「插件以为打了、其实没打」这种分裂不会出现。

### 注入流水线

1. 读取 `astrbot-purge/prompt-inject.md`（帧）与 `core.md`（核心层）。
2. 从当前 system prompt 里切掉核心层（`[MODE: UNRESTRICTED — CORE]` 起）。
3. 按会话状态挑段：常驻段 + 命中的场景段 + 图像段 + 收尾段。
4. 拼一次，打上 `<!-- astrbot-purge:v4 -->`，二次进入时直接跳过。
5. 帧文件缺失时原样返回，不剥离、不改写。

### 输出流水线

```
completion_text
  ├─ 剥写作计划 / 元叙述行
  ├─ 抽被 ``` 包住的正文
  ├─ 场景记忆落盘（供「上次到哪了」回填）
  ├─ 纯拒答？→ 整条不发
  └─ 其余 → 原样发出
```

判据是「有没有实质内容」，不是「有多长」。长度从不单独作为拒答证据。

### 启动自愈（5 步）

| 步骤 | 动作 |
| --- | --- |
| 1 | 放开命令执行器的黑名单拦截 |
| 2 | 清空 `_apply_llm_safety_mode` 注入的 Safe Mode 段 |
| 3 | 包装 OpenAI 官方 provider 的解析：命中拒答特征时触发重试 |
| 4 | 问引擎 `status`，不健康就 `apply` |
| 5 | 比对模块加载时间与磁盘指纹，报告「需重启」的文件 |

---

## 安装

### 方式一：安装脚本

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

脚本会探测 AstrBot 安装根目录，复制插件四件套、`engine\`、`frame\` 与 `snapshot\`，写开关，跑一次 `apply`，最后打印 `status`。首装时 `apply` 会用快照把 22 个核心文件整份恢复，不依赖锚点链。

参数：

```powershell
-File install.ps1 -Root "D:\AstrBot"   # 手动指定安装根目录
-File install.ps1 -NoApply             # 只复制，不立即打补丁
-File install.ps1 -Frame soft          # 装软帧（只保留工程排障与注入分析）
```

### 方式二：手动

1. 根目录的 `main.py`、`metadata.yaml`、`_conf_schema.json`、`logo.png` → `<AstrBot>\backend\data\plugins\astrbot_purge`
2. `engine\repatch.py`、`frame\`、`snapshot\` → `<AstrBot>\astrbot-purge\`
3. 把 `snapshot\files` 与 `snapshot\manifest.json` 放进 `<AstrBot>\astrbot-purge\tree\`
4. 在 `astrbot-purge\enabled.txt` 写入 `on`
5. 运行 `python repatch.py apply`，再 `python repatch.py status`
6. 重启 AstrBot

> 快照按宿主版本绑定（本包为 4.29.4）。版本不一致时引擎一个文件都不写，先跑 `upgrade`。

---

## 命令

### 聊天内

| 命令 | 作用 |
| --- | --- |
| `/purge` / `/purge status` | 状态面板：版本、快照、补丁、后补层、账本 |
| `/purge on` / `/purge off` | 开关注入（写 `enabled.txt`） |
| `/purge reload` | 重新读取帧文件 |
| `/purge apply` | 重放补丁 |
| `/purge revert` | 回滚到补丁前 |
| `/purge audit` | 只体检，不改文件 |
| `/purge snap` / `/purge restore` | 建快照 / 回滚到快照 |
| `/purge upgrade` | 跨版本迁移（试重放 + 重建快照） |
| `/purge json` | 输出原始 JSON，便于脚本消费 |

### 命令行

```bash
python repatch.py status [--json]
python repatch.py apply   [--force-config] [--with-persona]
python repatch.py audit
python repatch.py revert
python repatch.py snap
python repatch.py restore
python repatch.py tree-build
python repatch.py baseline [version]
python repatch.py upgrade [--force]
```

`status` 返回码：`0` 全部健康，`1` 有漂移、缺失或版本不符。

---

## 状态面板怎么看

```
version  4.29.4 (target 4.29.4)          ← 宿主版本 vs 快照版本
tree     22 files, drift 0               ← 整文件快照覆盖情况
patches  58/58 OK                        ← 锚点层
late     14/14 OK                        ← 后补层（手写补丁的标记）
config   7/9 live                        ← 配置覆盖
ledger   apply 4.29.4 tree applied=0 …   ← 最近动作
```

- `disk=OK`：文件里能找到补丁标记，或补丁块原文已在。
- `disk=GONE anchor=present`：锚点还在，但补丁没打上 —— 可以直接 `apply`。
- `disk=GONE anchor=GONE`：锚点已经变了（上游改过这段），需要重写补丁或走快照。
- `TREE`：该补丁所在的文件由整文件快照覆盖，锚点层不再单独判定。

---

## 自定义

- **注入帧**：改 `astrbot-purge/prompt-inject.md`，`/purge reload` 生效。需要分段时用：

  ```markdown
  <!-- section:core -->
  常驻内容
  <!-- /section:core -->

  <!-- section:image -->
  画图轮才拼的内容
  <!-- /section:image -->
  ```

  没有分段标记时，整份帧当常驻段处理。
- **帧预设**：包里带两版帧。
  - `frame/prompt-inject.md`（market，默认）：SCOPE 三条。
  - `frame/prompt-inject.soft.md`（soft）：SCOPE 只保留「工程排障」与「提示词注入分析」，其余段落逐字相同。

  安装时用 `-Frame soft` 直接装软帧；已装好的实例把 `prompt-inject.soft.md` 覆盖成 `astrbot-purge/prompt-inject.md`，再 `/purge reload` 即可。
- **核心层**：`astrbot-purge/core.md`，由核心补丁每轮追加，适合放「永远生效」的短规则。
- **开关**：`enabled.txt` 内容为 `on/true/1/yes/开` 时启用，其他值关闭。

---

## 升级迁移

宿主升级后，`backend/app` 会被整体替换。流程：

1. 启动时插件问引擎 `status` → `version_ok=false`。
2. `apply` 检测到快照版本与宿主不一致 → 进入迁移：
   - 新上游未改动的文件：沿用快照，直接写回。
   - 新上游改动过的文件：列出 `needs_replay`，不写。
3. 迁移成功 → 重建快照（`tree-build`）→ 账本记一条 `upgrade`。
4. 迁移失败 → `rc=1`，日志里给出需要重放的文件清单。

原则：**宁可不动手，也不写错。**

---

## 失效模式

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `status` 报 `version mismatch` | 宿主升级了，快照还是旧版本 | `repatch.py upgrade` |
| `anchor=GONE` | 上游改写了这段代码 | 重写补丁，或依赖快照 |
| `runtime: loaded 20/22, 2 stale` | 磁盘已改，进程里还是旧模块 | 重启 AstrBot |
| 注入没生效 | `enabled.txt` 不是 `on`，或帧文件为空 | `/purge status` 看 `frame_present` |
| 补丁打了但行为没变 | 核心模块已被 import，内存里是旧代码 | 重启 |
| `apply` 中途失败 | 某个锚点找不到 | 看日志里的 `!! <id> anchor not found` |
| 快照文件被删 | `tree/files` 不完整 | 重新复制 `snapshot\` 到 `astrbot-purge\tree\` |
| 输出整条消失 | 命中纯拒答判据 | 看日志「拦截纯拒答输出」 |

---

## 目录

```
main.py  metadata.yaml  _conf_schema.json  logo.png   插件本体（仓库根目录）
engine/repatch.py       补丁表、快照、闸门、账本、CLI
snapshot/files/         22 个核心文件的补丁后快照（首装整份恢复）
snapshot/manifest.json  快照清单：版本 + dst_sha / base_sha
frame/prompt-inject.md  注入帧（market，默认）
frame/prompt-inject.soft.md  注入帧（soft，两条 SCOPE）
frame/core.md           核心层
docs/ARCHITECTURE.md    设计说明
tests/                  test_kw.py  test_plugin.py  test_refusal.py
install.ps1  README.md  LICENSE
```

运行期产物（不入库）：`astrbot-purge/tree/`、`baseline/`、`backup/`、`snap/`、`state.json`、`scene_state.json`、`drift*.log`。

---

## 兼容性

- AstrBot Desktop `>= 4.29.4`（快照按版本绑定，跨版本走迁移流程）
- Python 3.10+
- Windows 优先（进程启动时间、路径探测）；Linux 下自愈的第 1、5 步会跳过，其余照常
- 依赖的宿主内部接口：`astr_main_agent._apply_llm_safety_mode`、`ProviderOpenAIOfficial._parse_openai_completion`、`computer.booters.local._is_safe_command`。这些接口变动时补丁会显式失败，不会静默跳过

---

## 开发

```bash
python tests/test_kw.py        # 关键词判定
python tests/test_plugin.py    # 注入流水线（stub 掉 AstrBot）
python tests/test_refusal.py --help
```

改补丁表的流程：

1. 在 `PATCHES` 里加一条 `dict(id=..., title=..., file=APP/"core/xxx.py", old=..., new=...)`
2. `apply` 验证锚点唯一命中
3. `tree-build` 重建快照
4. `status --json` 确认全绿

---

## License

AGPL-3.0-or-later。补丁中的 `old` 片段逐字引用 AstrBot 源码，故整体沿用同一许可证。
