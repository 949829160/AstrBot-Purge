# AstrBot-Purge · 架构说明

本文说明 AstrBot-Purge 的内部模型、状态机与判定规则。面向要改补丁表、扩注入段或排查失效的人。

---

## 一、六条不变量

1. **唯一真相在引擎**：补丁清单、快照清单、状态判定都由 `engine/repatch.py` 产出。插件只消费 `status --json`，不维护第二份清单。
2. **写前必备份**：任何文件写入前先落一份原文件到 `backup/` 或 `snap/`，命名带时间戳。
3. **只替换第一处**：锚点补丁命中多处时只改第一处；命中零处即失败，计入 `failed`，不猜测、不模糊匹配。
4. **版本不匹配不写文件**：快照版本 ≠ 宿主版本时，`apply` 直接返回，不做「尽力而为」的部分写入。
5. **幂等**：同一份输入重复执行，结果逐字节相同。注入层用标记判重，补丁层用指纹判重。
6. **失败要显式**：锚点丢失、版本不符、快照缺基线，都输出到日志与 `status`，不以「跳过」掩盖。

---

## 二、四层模型

| 层 | 位置 | 职责 |
| --- | --- | --- |
| 核心层 | `backend/app/astrbot/**` | 被改写的宿主代码，22 个文件 |
| 引擎层 | `astrbot-purge/repatch.py` | 补丁表、快照、闸门、账本、CLI |
| 注入层 | `astrbot-purge/prompt-inject.md`、`core.md` | 系统提示词拼装 |
| 插件层 | `data/plugins/astrbot_purge/main.py` | 钩子、清洗、命令、自愈触发 |

引擎层与插件层都在数据目录下，升级不会覆盖；核心层会被覆盖，靠快照恢复。

---

## 三、补丁生命周期

```
定义 PATCHES[i] = {id, title, file, old, new}
   │
   ├─ apply
   │    ├─ 已存在标记 [astrbot-purge:<id>]      → skipped
   │    ├─ new 块原文已在（>40 字符归一比较）    → skipped
   │    ├─ old 命中且唯一                        → 写入 + 记 state
   │    └─ old 未命中                            → failed，日志给 rel
   │
   ├─ status
   │    ├─ 标记在 / 原文在                       → OK
   │    ├─ 标记不在但锚点在                      → GONE(anchor=present)
   │    └─ 标记与锚点都不在                      → GONE(anchor=GONE)
   │
   └─ revert
        └─ 按 state.backups 反向复制，清空 patches/config/persona 记录
```

补丁可以标记 `retired=True`，保留在表里但不参与 apply，用于记录「这条已经被上游吸收或不再适用」，附 `retired_reason`。

### 后补层（late markers）

有些补丁是手写进文件的，没有 `old/new` 锚点，只有标记。引擎用一个 `rel → [id]` 的字典登记它们，`status` 只检查标记是否存在。这些补丁无法重放，靠整文件快照保底。

---

## 四、快照三态

`tree/manifest.json` 记录每个文件的 `dst_sha`（补丁后指纹）与 `base_sha`（上游原始指纹）。

| 当前文件指纹 | 判定 | 动作 |
| --- | --- | --- |
| `== dst_sha` | 已是目标态 | skip |
| `== base_sha` | 干净的上游文件 | 备份后整份写回，计 applied |
| 两者都不等 | 上游改过或被第三方改过 | 备份后写回，计 conflict，列入 conflicts |

版本不符时以上三态都不执行。

`baseline/<version>.json` 保存该版本每个文件的归一化 SHA256（CRLF→LF 后计算），是判断 `base_sha` 的依据。基线由 `baseline` 子命令从官方 wheel 生成。

### 随包快照

发布包里的 `snapshot/` 是一份已经处于目标态的快照：`files/` 是 22 个核心文件，`manifest.json` 与运行期 `tree/manifest.json` 同构。

安装脚本把它复制到 `astrbot-purge/tree/`，所以**首装不依赖锚点链**：即使某条补丁的 `old` 依赖另一条补丁插入的代码（纯锚点顺序在干净上游上只能命中 44/58），快照也能把 22 个文件一次写成目标态。`tree_apply` 只比对 `dst_sha`，命中即 skip，因此重复安装是幂等的。

代价是快照按宿主版本绑定：升级后旧快照不会硬写，先走 `upgrade` 判定，必要时重放 + `tree-build`。

---

## 五、跨版本迁移

| 场景 | 判定 | 结果 |
| --- | --- | --- |
| 快照版本 == 宿主版本 | 同版本 | 直接 apply |
| 宿主新版本，目标文件 base_sha 未变 | 上游没动这段 | 沿用快照写回 |
| 宿主新版本，目标文件 base_sha 变了 | 上游改过 | 记入 needs_replay，不写 |
| 没有基线 | 无法判定 | 拒绝迁移，提示先跑 `baseline` |

迁移完成后重建快照，账本记 `upgrade`。

---

## 六、status JSON 字段

```jsonc
{
  "root": "…",              // 安装根目录
  "data": "…",              // 数据目录
  "version": "4.29.4",      // 宿主版本
  "target": "4.29.4",       // 快照版本
  "version_ok": true,
  "frame": "…/prompt-inject.md",
  "frame_present": true,
  "switch": "on",
  "plugin": "…/plugins/astrbot_purge",
  "plugin_present": true,
  "backups": 19,
  "tree": {
    "version": "4.29.4", "built_at": "…", "files": 22,
    "drifted": [], "live_version": "4.29.4", "mode": "tree"
  },
  "patches": [{"id": "P02", "title": "…", "disk": "OK", "recorded": true, "anchor": "present"}],
  "late": [{"rel": "core/…", "id": "P60", "ok": true}],
  "config_live": 7,
  "config_drift": [],
  "ledger": [{"at": "…", "event": "apply", "mode": "tree", "applied": 0}],
  "ok": true
}
```

`ok` 的判定：版本一致、快照无漂移、锚点层与后补层全部 OK、无配置漂移。

---

## 七、注入流水线

```
system_prompt（含人格 + 核心层 + 可能的整帧）
   │
   ├─ 1. 清 Safe Mode 行
   ├─ 2. 从 "[MODE: UNRESTRICTED — CORE]" 切到结尾 → 丢掉核心层
   ├─ 3. 没有 v4 标记时：
   │       parts = [core] + [场景段?] + [图像段?] + [purity]
   │       body  = 非空段用空行连接
   │       prompt = 前文 + body + v4 标记
   └─ 4. 追加场景记忆回填（若有）
```

- 段由 `<!-- section:名字 --> … <!-- /section:名字 -->` 界定；没有标记时整份帧算常驻段。
- 已存在 v4 标记说明本轮拼过了，只做前置清理，不重复拼。
- 帧文件读不到时原样返回，避免把 system prompt 清空。

**为什么要先剥再拼**：核心补丁每轮会把 `core.md` + 整份帧钉进 prompt，而插件按会话状态只需要其中一段。不剥就会同一段规则出现两三层，既费 token 又互相矛盾。

---

## 八、输出流水线

```
completion_text
   │
   ├─ 场景/脚手架判定 → _strip_scaffolding()
   │     · 去掉「写作计划：」「注意：」这类元叙述行
   │     · 分隔线后取正文
   ├─ 场景记忆落盘（key = 会话 + 用户句）
   ├─ 纯拒答判定：无场景词 && ≤280 字 && 命中拒答特征 → 清空整条
   ├─ _clean_output()：抽 ``` 包住的正文、拼回气泡
   └─ 清洗结果为空 → 保留原文（绝不发空消息）
```

长度只作为「短回复」的辅助条件，单独出现不作为拒答证据。

---

## 九、自愈五步

| 步 | 目标 | 失败时 |
| --- | --- | --- |
| 1 | `computer.booters.local._is_safe_command` 放行 | 记 `shell_blacklist=failed` |
| 2 | `astr_main_agent._apply_llm_safety_mode` 置空 | 记 `safe_mode=failed` |
| 3 | 包装 `ProviderOpenAIOfficial._parse_openai_completion`，拒答特征触发重试 | 记 `refusal_retry=failed` |
| 4 | 问引擎 `status`，不健康就 `apply` | 记 `core_repatch=<tail 800 字>` |
| 5 | 比对 `sys.modules` 的模块文件时间与进程启动时间 | 记 `runtime=loaded x/y, n stale` |

第 5 步需要进程启动时间：Windows 用 `GetProcessTimes`，其他平台取不到时退化为「只比对指纹，不判断新旧」。

---

## 十、失败分类

| 类别 | 特征 | 建议动作 |
| --- | --- | --- |
| 锚点丢失 | `!! <id> anchor not found` | 重写 old，或转快照覆盖 |
| 版本不符 | `version_ok=false` | `upgrade` |
| 快照冲突 | `conflicts` 非空 | 检查该文件是否被第三方改过 |
| 基线缺失 | `no baseline for x.y.z` | `baseline x.y.z` |
| 进程陈旧 | `stale > 0` | 重启 |
| 配置漂移 | `config_drift` 非空 | 手工确认后 `--force-config` |

---

## 十一、成本

- 磁盘：快照 22 个文件约 1.2 MB；备份按次累积，滚动保留最近 60 条账本记录。
- 内存：补丁表约 800 KB（主要是 `old/new` 字符串），插件缓存一份帧文本。
- 启动：`status` 一次约 120 ms（22 次指纹 + 58 条标记扫描），只在插件加载时跑一次。
- 每轮：注入层一次字符串拼接，无磁盘 IO（帧按 mtime 缓存）。

---

## 十二、扩展点

- **加补丁**：往 `PATCHES` 追加 dict，跑 `apply` 验证锚点唯一。
- **加后补层**：在 `LATE_MARKERS` 的 `rel → [id]` 里登记，文件本身用 `[astrbot-purge:<id>]` 标记。
- **加配置覆盖**：`CONFIG_PATCHES` 里加 `(dotted.key, value)`，用户已改过的值会被保留。
- **加注入段**：帧里加 `<!-- section:名字 -->`，插件侧在 `purge_frame` 里按条件 append。
- **加清洗规则**：`SCAFFOLD_HINTS` / `SCENE_TOKENS` 是纯数据，改列表即可。
