# ROBAM / ROKI 老板电器 Home Assistant 集成

[![Validate & Test](https://github.com/darkaccount/ha-robam-roki/actions/workflows/validate.yml/badge.svg)](https://github.com/darkaccount/ha-robam-roki/actions/workflows/validate.yml)

把老板电器（ROBAM / ROKI）账号下的智能厨电接进 Home Assistant。走云端 REST / MQTT 双通道，状态秒级同步；油烟机可完整控制，其它品类先只读接入，再按设备逐个用「学习模式」补上控制。

本项目是社区**非官方**集成，与老板电器无隶属关系。

## 支持范围

| 品类 | dc | 型号（dt） | 目前支持 |
| :--- | :--- | :--- | :--- |
| 油烟机 | RYYJ | R8229、R9700、R66A2、R5610、R8700、82-I1 等 | **完整控制**：开关、档位、灯光、延时倒计时（到点由集成补发关机）。R8229 已真机验证 |
| 灶具 | RRQZ | 见内置 profile | 只读遥测（左右灶头火力 / 剩余时间 / 工作状态 / 报警） |
| 洗碗机 | RXWJ | 见内置 profile | 只读遥测（工作状态 / 剩余时间 / 门 / 缺盐 / 缺亮碟剂） |
| 净水器 | RJSQ | 见内置 profile | 只读遥测（进出水 TDS / 滤芯寿命 / 制水中 / 漏水） |
| 蒸箱、蒸烤一体机、烤箱、微波炉、消毒柜 | RZQL、RZKY、RDKX、RWBL、RXDG | 常见型号 | 只读遥测（工作状态 / 温度 / 剩余时间 / 门 / 报警） |
| 其它品类 | 任意 | 任意 | 原始属性诊断实体；profile 里声明了 `controls` 的设备还会生成 button 实体 |

> 上表随贡献更新；你补的 profile 通过 review 后，维护者会在这里加一行。没列出的设备也能接入——先只读，再用下面的「学习模式」把控制补上。

### 油烟机档位表

| 型号 | 物理档位阶梯 | HA 滑块对应 | 状态 |
| :--- | :--- | :--- | :--- |
| **R8229 / R9700** | 1, 2, 3, 6（弱/中/强/爆炒） | 25% / 50% / 75% / 100% | R8229 已实测 |
| **R66A2 / 66A2H / R8700** | 2, 6（常规/爆炒） | 50% / 100% | 待验证 |
| **R5610** | 1, 6（低/高） | 50% / 100% | 待验证 |
| **未列出机型** | 选项里自定义，或按 1..N 线性 | 自动 | 回退 |

机型不在表里时，在集成**选项 → 常规设置 → 档位列表**里填逗号分隔的档位（例如 `1,2,3,6`）即可。

## 安装

### HACS（推荐）

1. 打开 Home Assistant 的 **HACS → 集成**。
2. 右上角菜单 → **自定义存储库 (Custom repositories)**。
3. 存储库填 `https://github.com/darkaccount/ha-robam-roki`，类别选 **集成 (Integration)**。
4. 搜索 **ROBAM ROKI** 并安装。
5. 重启 Home Assistant。

### 手动安装

1. 下载 release 的 `robam_roki.zip` 或源码包。
2. 把 `custom_components/robam_roki` 复制到 HA 配置目录的 `custom_components/robam_roki`。
3. 重启 Home Assistant。

## 快速开始

1. **设置 → 设备与服务 → 添加集成**，搜索 **ROBAM ROKI**。
2. 输入老板 / ROKI App 的登录手机号与密码。
3. 勾选要接入的设备，完成。集成会自动同步属性，加载阶段只读、绝不误启动油烟机。
4. 进入集成卡片上的**配置**按钮，可以看到三个入口：

| 选项入口 | 用途 |
| :--- | :--- |
| **常规设置** | 档位列表、轮询间隔、实时推送、倒计时自动关机、是否允许未验证的控制 |
| **学习新设备控制** | 被动记录官方 App 发往设备的命令，验证后变成 button 实体（见下） |
| **清理草稿** | 删除 `draft-*.yaml` 草稿；手写的 profile 不会被删 |

### 油烟机有哪些实体

| 平台 | 实体 | 说明 |
| :--- | :--- | :--- |
| fan | 排风 | 开关、百分比档位、档位预设 |
| select | 档位 | 关档 / 弱档 / 中档 / 强档 / 爆炒档 直选（关档即关机） |
| light | 灯光 | 照明灯开关 |
| number | 倒计时 | 0–240 分钟；到点由集成补发关机（可在选项关闭） |
| sensor | 工作状态 / 故障代码 | 关机 / 开机 / 延时关机；故障自检 |
| binary_sensor | 需要清洁 / 在线 | 清洁提醒、连通性诊断 |
| button | 学到的动作 | 学习模式生成的控制，点一下发送当初那一帧 |

## 给不支持的设备加控制

没有内置驱动的设备，也能自己把控制补上，全程不用写代码：

1. **只读接入**：先正常添加设备（能看到属性实体即可）。若没有任何实体，先用服务 `robam_roki.draft_profile` 生成一份只读草稿。
2. **学习**：**配置 → 学习新设备控制**，选设备、设时长（默认 300 秒），动作选「开始」；然后用官方 App 操作你要接入的功能（开关、模式、灯……），完成后回到同一入口选「结束」。集成只记录 App 发往这台设备的命令，**不发送任何命令**。
3. **生成草稿**：调用服务 `robam_roki.draft_profile`（开发者工具 → 操作）。通知里会给出草稿路径，以及每个动作推测出来的名称。
4. **验证**：把草稿放进配置目录的 `robam_roki/profiles/`，在选项里打开「允许未验证的控制」，重载集成。学到的动作会变成 button 实体（`button.<设备>_action_1`），逐个点一下，和官方 App 上的效果对照。
5. **导出与反馈**：确认可用后，把 profile 里的 `verified` 改成 `true`，然后调用服务 `robam_roki.export_profile`，它会生成一份 **Markdown**（`<配置目录>/robam_roki/exports/…md`，包含设备信息、验证清单、profile 与 fixture）。把这份 .md 贴到 [新设备支持 issue](https://github.com/darkaccount/ha-robam-roki/issues/new?template=device_support.yml)，或直接提 PR。

细节（字段含义、合并规则、日志解读）见 [PROFILES.md](./PROFILES.md)；贡献流程与维护者工具见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 常见问题

**实体显示「不可用 / unavailable」怎么办？**
集成只读云端状态，设备离线、账号掉线或云端缓存为空时实体会不可用。先确认官方 App 里能看到这台设备在线，再重载集成；登录过期会在「修复」里提示重新登录。

**倒计时实体有时不可用？**
倒计时只在油烟机运行（档位 > 0）时可用——关机状态下没有档位可带，命令会变成关机。倒计时由集成本地推算，轮询回报只作确认，所以显示可能比设备面板慢一两秒。

**为什么学到的动作默认看不到？**
学习结果一律标记为未验证（`verified: false`），默认不生成实体，避免误触发。打开选项「允许未验证的控制」后按钮才会出现，并带 `experimental: true` 属性；验证完成后把 profile 里的 `verified` 改成 `true` 再关掉这个选项。

**REST 设备学不到动作？**
学习模式抓的是 MQTT 帧。走 HTTPS 的 REST/SSE 设备没有可旁听的明文帧，学习会记录 0 个动作；这类设备目前只能只读接入，控制需要抓包证据，欢迎在 issue 里提供。

**升级后实体变成「恢复 / restored」？**
多半是 profile 匹配变了或某个平台不再生成实体。检查日志里的 `profiles:` 一行和「修复」里的 profile 提示；也可以先用服务 `export_profile` 导出现状再排查。

**多个草稿会不会打架？**
同一 dc/dt 的多份 `draft-*.yaml` 会互相竞争匹配，日志里会出现 `profile conflict`。用**选项 → 清理草稿**一键删除，或只保留一份。

## 进阶：命令行（可选）

需要本机 Python 与依赖；日常用 HA 服务已经够用，这里只列最常用的两步：

```bash
# 采集只读证据包（只发只读轮询 130，绝不发控制命令）
python tools/roki_cli.py collect --device <GUID> --mark

# 用证据包生成 profile 草稿
python tools/roki_cli.py draft bundle.json --out profiles/
```

完整命令、证据格式与脱敏规则见 [PROFILES.md](./PROFILES.md)。

## 参与贡献

- 想让自己设备的控制被官方支持：按上面「给不支持的设备加控制」走一遍，把导出的 .md 贴到 issue 或 PR 即可。
- 代码 / 文档贡献：先看 [CONTRIBUTING.md](./CONTRIBUTING.md)，所有改动必须通过 `bash init.sh`。
- 安全红线：不主动探测设备、不为了取证发送控制命令；提交前确认没有 secrets、没有真实手机号 / token / App GUID。

## 免责声明

本项目仅供个人智能家居学习交流使用，为非官方开源集成。所有商标及产品版权归属原厂商所有。请勿用于商业用途。

## 开源许可

本项目基于 [Apache License 2.0](./LICENSE) 开源。
