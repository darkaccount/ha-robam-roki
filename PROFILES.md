# YAML profiles（设备描述与实验性控制）

本文说明 `custom_components/robam_roki/profiles/` 与用户目录里的 YAML profile：它解决什么问题、文件放哪、字段怎么填、冲突时谁赢、日志怎么读。

> **0.5.0 起 profile 可以声明控制**：`controls` 段支持 `kind: property` / `invoke` / `mqtt`；学习模式学到的动作会自动写成 `kind: mqtt`，并以 **button（按钮）** 实体出现，点一下发送那一帧（详见 4.6）。未验证的控制（`verified: false`）默认**不生成实体**，需要先在集成选项里打开「允许未验证的控制」，生成出来的实体会带 `experimental: true` 属性。没有 `controls` 时行为与以前一致：只生成 sensor / binary_sensor，不发任何命令。

## 1. 它解决什么问题

- 云端已经上报属性、但集成还没有对应 Python 驱动的品类：用 profile 直接生成结构化只读实体（`raw_state` 之外）。
- 某个内置驱动在你的机型上名称或取值不合适：用 profile 继承它并覆盖个别条目，不必改代码。

## 2. 目录、来源与生效时机

| 位置 | 用途 | 更新行为 |
| :--- | :--- | :--- |
| `custom_components/robam_roki/profiles/` | 集成内置示例 | 随集成更新被替换，不要把自己的文件放这里 |
| `<HA 配置目录>/robam_roki/profiles/` | 用户 profile | 升级保留，你的文件放这里 |
| `_shared/` 子目录（以上两处均可） | 片段（fragment） | 可省略 match，供 `extends` 引用 |

- 只读取 `*.yaml`（`.yml` 不会被读取）。
- 修改后重载集成（或重启 HA）生效。
- 每次启动会打印一行 INFO 日志：

~~~text
profiles: builtin=1 user=0 (builtin=/.../custom_components/robam_roki/profiles, user=/config/robam_roki/profiles (missing))
~~~

  user 目录不存在时后面带 `(missing)`，会直接显示集成实际查找的路径——放错位置一眼可见。
- 单个文件损坏只影响该文件（以及引用它的 profile），不会中断集成启动。

## 3. 最小可用示例

`<配置目录>/robam_roki/profiles/my-oven.yaml`：

~~~yaml
schema: roki-profile/1
match:
  dc: RZQL
sensors:
  cavity_temp:
    keys: [curTopTemp, topTemp]
    type: float
    unit: °C
    device_class: temperature
    state_class: measurement
    name: 腔体温度
~~~

- `id` 省略时默认取文件名（这里是 `my-oven`）。
- 实体 unique_id 固定为 `{设备 GUID}_{key}`，与内置驱动一致；改 profile 不会改变实体 ID。

## 4. 字段参考

### 4.1 顶层字段

| 字段 | 必填 | 类型 | 说明 |
| :--- | :--- | :--- | :--- |
| `schema` | 是 | 字符串 | 必须是 `roki-profile/1` |
| `id` | 否 | 字符串 | profile 标识；缺省用文件名，供 `extends` 引用 |
| `extends` | 否 | 字符串 | 片段 id，或 `python:<驱动名>` |
| `match` | 非 `_shared/` 文件必填 | 映射 | 设备匹配条件 |
| `name` | 否 | 字符串 | profile 名称（设备类别名，也用于继承时的回退） |
| `sensors` | 否 | 映射 | 每个 key 一个传感器条目 |
| `binary_sensors` | 否 | 映射 | 每个 key 一个二元传感器条目 |
| `controls` | 否 | 映射 | 每个 key 一个控制条目；可执行的那几条以 button 实体出现（见 4.6） |

### 4.2 match

| 字段 | 必填 | 类型 | 说明 |
| :--- | :--- | :--- | :--- |
| `dc` | 写了 match 时必填 | 字符串 | 设备品类码，如 `RZQL`、`RYYJ` |
| `dt` | 否 | 字符串或字符串列表 | 型号码，如 `C98A` 或 `[C98A, C98B]` |

### 4.3 sensors 条目

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `keys` | 字符串或列表 | 属性多键回退；**新建实体时必需，覆盖已有实体时可省**（省 = 只改展示，沿用驱动取值） |
| `type` | `int` / `float` / `str` / `bool` | 取值转换 |
| `scale` | 数字 | 转换后乘系数，如 `0.1` |
| `map` | 映射 | 取值 → 显示值；未命中用 `default` |
| `default` | 任意 | 无属性、转换失败或 map 未命中时的回退 |
| `name` | 字符串 | 实体名称；设置后优先于翻译表（不再使用 translation_key） |
| `icon` | 字符串 | 如 `mdi:thermometer` |
| `unit` | 字符串 | 如 `°C`、`W`、`min` |
| `device_class` | 字符串 | HA 设备类，如 `temperature`、`power` |
| `state_class` | 字符串 | 如 `measurement`、`total` |
| `entity_category` | `diagnostic` / `config` | 实体分类 |

取值顺序：`keys` 里第一个非空属性 → `type` 转换 → `scale` → `map`（未命中 → `default`）。

### 4.4 binary_sensors 条目

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `keys` | 字符串或列表 | 同上（新建必需，覆盖可省） |
| `on_values` | 列表 | 属性值命中这些值时为 on；省略则按真值判断 |
| `name` / `icon` / `device_class` / `entity_category` | 同上 | 展示字段 |

### 4.5 controls（控制能力）

字段与 `custom_components/robam_roki/devices/base.py` 的 `CommandSpec` 对齐。控制条目只有在 `verified: true`，或用户打开了「允许未验证的控制」选项时才会生成实体；至于生成成什么实体，由 `kind` 决定，见 4.6。

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `kind` | `property` / `invoke` / `mqtt` | 必填，缺省或非法时该条目被跳过并记日志 |
| `name` | 字符串 | 可选，按钮实体的显示名；省略时用 key（例如学习生成的 `action_1`） |
| `on` / `off` | 映射 | `property`：开 / 关时写入的属性与值 |
| `function` | 字符串或整数 | `invoke`：云端协议函数号 |
| `param` | 字符串 | `invoke`：把实体传入的值放到这个参数名 |
| `params` | 映射 | `invoke`：固定附加参数 |
| `command` | 整数（或数字字符串） | `mqtt`：命令字节，取值 0–255 |
| `body_hex` | 十六进制字符串 | `mqtt`：帧体；偶数长度、≤ 64 字节；外层信封（源地址、terminal、user id）由集成补齐 |
| `verified` | 布尔，默认 true | 是否已在真机验证；false 时受实验开关控制 |

示例（MQTT 设备；key 建议避开 `power`/`level`/`light`/`timing`，那四个由专用平台负责，写成同名会覆盖它们的命令而不是多出一个按钮）。以下只是 `controls` 片段，必须和 `schema: roki-profile/1`、`match` 一起组成完整 profile 才能加载，见 §3：

~~~yaml
controls:
  lamp:
    kind: mqtt
    command: 132
    body_hex: "01"
    verified: false
~~~

示例（REST 设备，同样只是 `controls` 片段）：

~~~yaml
controls:
  cavity_light:
    kind: property
    on: {light_status: 1}
    off: {light_status: 0}
    verified: false
~~~

YAML 小坑已由加载器处理：PyYAML 会把裸写的 `on:` / `off:` 解析成布尔键，加载时归一化回 `"on"`/`"off"`；`param: on` 这类布尔标量也会还原成 `"on"`。加不加引号等价。

### 4.6 controls 会生成什么实体

控制条目按 `kind` 决定有没有实体。能「一键发送」的变成 **button（按钮）**，需要传值的和走 REST 写接口的暂时只保留声明，不生成实体：

| `kind` | 生成的实体 | 说明 |
| :--- | :--- | :--- |
| `mqtt` | **button** | 点一下发送这一帧（命令号 + `body_hex`） |
| `invoke`，没写 `param` | **button** | 点一下调用这个云端函数，`params` 原样附上 |
| `invoke`，写了 `param` | 暂无 | 需要用户先输入一个值，专用平台尚未实现 |
| `property` | 暂无 | 走云端属性写接口（REST 通道），实体形态尚未实现 |

- 按钮实体的 `unique_id` 是 `{设备 GUID}_control_{key}`，与 sensor 的 `{设备 GUID}_{key}` 区分开，不会互相顶掉；实体名取 `name`，没有就用 key。
- `power` / `level` / `light` / `timing` 这四个 key 由 fan / select / light / number 平台负责，**不会**再生成按钮，同一能力不会重复出现。
- `verified: false` 的控制只有在打开「允许未验证的控制」后才会出现，且按钮的 `extra_state_attributes` 带 `experimental: true`。这是实体属性，不是 profile 字段，写进 YAML 会被忽略。
- 声明控制不会影响原有实体：`extends: python:<驱动>` 的 profile 会保留驱动全部命令（油烟机仍有风机/灯光/档位/倒计时）；不写 `controls` 的只读 profile `commands()` 仍然为空。
- 参数的学习结果示例（学习模式自动生成的就是这一种；同样只是 `controls` 片段）：

~~~yaml
controls:
  action_1:
    kind: mqtt
    command: 132
    body_hex: "01"
    verified: false
~~~

重载集成后，打开实验开关即可看到学到的按钮实体（实体名取 `name`，本例没写 `name`，所以显示为 `action_1`）；点一下发送命令 132，帧体 `01`。

## 5. 优先级与合并规则

1. **用户 > 内置**；
2. 同一来源内：`dt` 精确匹配 > `dt` 列表包含 > 仅 `dc`；
3. 同精度冲突：记 warning，按文件名排序取第一个；
4. `extends` 合并：按 section（sensors / binary_sensors / controls）浅合并，**同名 key 整条替换**，不递归合并字段；`controls` 对内置驱动的命令表也是同一语义——同名 key 用 profile 的整条命令覆盖驱动的那一条（例如把 `level` 换成自己抓到的帧），其它命令原样保留；
5. profile 覆盖驱动已有 key 时：只替换你写出的字段。只写 `name`/`icon` 等展示字段 → 取值逻辑保持驱动原样；写了 `keys` → 取值逻辑也由 profile 接管；
6. `extends: python:<name>` 继承内置驱动，可用名称：`hood`、`thermal`、`steam_oven`、`oven`、`microwave`、`sterilizer`、`generic`（灶具/洗碗机/净水器自 0.5.0 起由内置 YAML profile 提供，不再有同名 Python 驱动）。

## 6. 三个完整示例

### 6.1 新品类只读（无 Python 驱动）

适用于云端已上报属性、集成只有 `raw_state` 的品类：

~~~yaml
schema: roki-profile/1
id: my-coffee-machine
match:
  dc: RCOF
sensors:
  water_level:
    keys: [waterLevel, water]
    type: int
    unit: "%"
    name: 水箱水位
  brew_temp:
    keys: [brewTemp]
    type: float
    unit: °C
    device_class: temperature
    state_class: measurement
    name: 冲泡温度
binary_sensors:
  tank_missing:
    keys: [tankMissing]
    on_values: [1, true]
    device_class: problem
    name: 水箱未安装
~~~

### 6.2 片段继承（_shared）

`<配置目录>/robam_roki/profiles/_shared/base.yaml`：

~~~yaml
schema: roki-profile/1
id: base
sensors:
  fault_code:
    keys: [faultCode]
    type: int
    name: 故障代码
    entity_category: diagnostic
~~~

`<配置目录>/robam_roki/profiles/coffee.yaml`：

~~~yaml
schema: roki-profile/1
extends: base
match:
  dc: RCOF
sensors:
  water_level:
    keys: [waterLevel]
    type: int
    unit: "%"
    name: 水箱水位
~~~

合并结果：`fault_code` + `water_level`；同名 key 以子 profile 为准。

### 6.3 覆盖内置驱动

与集成自带的 `custom_components/robam_roki/profiles/thermal-override.yaml` 同构：

~~~yaml
schema: roki-profile/1
id: thermal-example
extends: python:thermal
match:
  dc: RZQL
sensors:
  top_temperature:
    name: 上管温度
~~~

没有写 `keys`，所以只改名称，取值仍由 thermal 驱动完成。

## 7. 常见错误与日志解读

| 日志关键字 | 含义 / 处理 |
| :--- | :--- |
| `unsupported schema` | `schema` 不是 `roki-profile/1`（包括只写了 `controls` 片段、没写 `schema`），整个文件被跳过 |
| `match.dc is required` | 写了 `match` 但没有 `dc`，整个文件被跳过 |
| `device profile needs match.dc` | 非 `_shared/` 文件没有 match，被跳过 |
| `unknown field ... ignored` | 字段拼错或未支持，忽略该字段继续加载 |
| `non-empty string expected` / `number expected` / `list expected` / `mapping expected` | 字段类型不对，忽略该字段 |
| `has no usable fields, skipped` | 条目里所有字段都无效，整个条目被跳过 |
| `extends ... not found` | `extends` 指向的片段 id 不存在，该 profile 被跳过 |
| `extends chain loops back` | `extends` 链循环，该 profile 被跳过 |
| `unknown python driver` | `extends: python:<name>` 名字不在支持列表，退回 profile 自身条目 |
| `invalid YAML` / `cannot read profile` | 文件损坏或读不到，只影响该文件 |
| `profile conflict` | 同来源、同精度多个 profile 同时命中，按文件名取第一个 |
| `profiles: builtin=... user=...` | 启动时的 profile 统计；user 后面带 `(missing)` 说明目录不存在 |
| `unknown device_class` / `unknown entity_category` | HA 枚举值写错，该字段被忽略 |
| `control ... skipped` / `invalid control` | 某条 `controls` 条目无法变成命令（`invoke` 缺 `function`、`mqtt` 缺命令号或 `body_hex` 非法），只跳过这一条，设备其它命令照旧 |
| `kind=property needs on and off` | `property` 控制必须同时写 `on` 与 `off`，否则只跳过该条 |

## 8. 安全边界

- profile 的写能力**只来自显式声明**：只有 `controls` 里写出的 `kind: mqtt`（或未带 `param` 的 `invoke`）才会生成 button 实体，并且只在**按下按钮**时发送命令。不写 `controls` 的 profile（内置的灶具 / 洗碗机 / 净水器就是这种）`commands()` 为空，不生成任何控制实体，也不会向设备发送命令。
- 加载 profile 只生成实体：setup、重载、生成草稿都不发送任何命令。
- 未验证的控制默认隐藏：`verified: false` 的控制不生成实体；即使有旧实体尝试发送，执行器也会拒绝，除非打开「允许未验证的控制」。
- 不主动探测设备：取值只读 coordinator 已经缓存的属性。
- 未知属性名不会报错，只是取不到值时按 `default`/`None` 处理；单条控制写坏只会跳过那一条并记 warning，不影响设备其它命令。

## 9. 自助接入（不用写代码）

拿到 profile 有下面几条路径，按门槛从低到高：

### 9.1 学习模式（推荐，先学再做）

1. **配置 → 1. 学习新设备控制**，选设备与时长（默认 300 秒，最长 600 秒），动作选「开始」。（自动化里也可以调服务 `robam_roki.learn_start`。）
2. 用官方 App 操作你要接入的功能（开关、档位、模式……）。集成只记录**官方 App 发往这台设备**的命令帧：设备自己的上报、以及本集成自己发出的命令都会被排除；学习期间**不发送任何命令**。
3. 最长 600 秒后**自动结束**，也可以回来选「结束」提前停。通知里会列出学到几个动作、命令号与 `body_hex`、以及每组观察到的属性变化。
4. **配置 → 2. 生成草稿**：草稿会直接写进 `<配置目录>/robam_roki/profiles/`，`controls` 逐条自动生成（`verified: false`，每个动作一条 `action_N`，名称按观察到的变化或载荷字节推测），不用手抄命令号。
5. 在确认页勾选「开启允许未验证的控制并重载」，学到的动作会以 **button** 实体出现（见 4.6）；点一下就是官方 App 当初发的那一帧。确认可用后把对应条目的 `verified` 改成 `true` 再分享。

如果学习了 0 个动作（例如 REST 设备，App 走 HTTPS 抓不到），通知会给出命令行 fallback 的完整命令。

### 9.2 生成草稿

**界面入口（推荐）**：**配置 → 2. 生成草稿**，选设备即可；草稿写入后可以在确认页一键开启实验开关并重载。

**服务入口（自动化用）**：开发者工具 → 操作 → `robam_roki.draft_profile`，`device_id` 与 `guid` 二选一，可选 `write_fixture`（默认开启，同时写回归 fixture）。

前提：集成已经添加并正常轮询。草稿只使用**已缓存**的属性，不会为了生成草稿请求云端，也不会向设备发命令。调用服务时，通知会先给文件路径与下一步，最后附整份 YAML 全文。

生成位置：

| 文件 | 路径 |
| :--- | :--- |
| profile | `<配置目录>/robam_roki/profiles/draft-<dc>-<dt>.yaml` |
| fixture | `<配置目录>/robam_roki/fixtures/<dc>-<dt>.json` |

同名文件不会被覆盖：再次生成会追加 `-2`、`-3`。草稿、fixture 和导出包会在配置目录里越积越多，用 **配置 → 4. 清理草稿** 可以分别勾选删除（默认只勾草稿，手写的 profile 不会被删）。

下一步：编辑草稿（把注释建议改成真实字段、按需把布尔属性移入 `binary_sensors`）→ 设置 → 设备与服务 → ROBAM ROKI → 重新加载 → 查看实体。草稿里的 `controls` 如果是学习生成的 `kind: mqtt`，重载后会变成 button 实体（需先打开「允许未验证的控制」，见 4.6）。

### 9.3 命令行（高级 / 可选）

需要本机 Python、依赖和 ROKI 云端账号凭据，**不需要 Home Assistant**。凭据用 `--phone/--password` 或环境变量 `ROKI_PHONE/ROKI_PASSWORD` 提供；都没有时，第一次联网任务会提示输入（密码不回显）。本节命令里的 `profiles/`、`fixtures/`、`exports/` 是运行命令时当前目录下的贡献用目录，不是 HA 的 `<配置目录>/robam_roki/`：

~~~bash
pip install -r tools/requirements.txt
python tools/roki_cli.py        # 不带参数：进入交互菜单
~~~

主链四步（菜单按此顺序排列）：

1. 读设备信息（只读）
2. **生成配置**（菜单 3）—— 用属性生成 `profiles/<dc>-<dt>.yaml` 与 `fixtures/<dc>-<dt>.json`，继承内置驱动、跳过已覆盖的键；只读设备到此为止
3. **采集并合并**（菜单 4）—— App 操作 → 回车结束（120 秒上限）→ 命令帧自动合并进已有配置（按 command + `body_hex` 去重，保留 sensors / 注释 / 手工改动），没有配置则新建
4. **导出贡献包**（菜单 5）—— 五节 `.md`，导出前询问 verified

子命令供自动化使用，参数与行为与菜单一致：

~~~bash
# 1) 生成配置：只读设备信息，不发任何控制命令
python tools/roki_cli.py config init <GUID> --out profiles/ --fixtures-dir fixtures/
#    已存在同名文件时询问；--json 或非交互运行时追加 -2，不覆盖

# 2) 采集：只发布只读状态查询，绝不发控制命令
python tools/roki_cli.py collect --device <GUID>
#    --duration 默认 120s 被动采样；--mark 交互打点（回车结束）
#    除了设备上报，也会记录官方 App 发出的命令帧（commands 段）：
#    采集阶段就剥离 terminal 与账号 id，bundle 里不会出现账号信息

# 3) 合并：把采集到的命令并进已有配置（也可不合并，直接生成新草稿）
python tools/roki_cli.py draft bundle.json --merge profiles/RYYJ-R8229.yaml
#    保留 sensors / 注释 / 手工改动；按 (command, body_hex) 去重，追加新的 action_N
python tools/roki_cli.py draft bundle.json --out profiles/

# 4) 导出：五节 .md，与 HA 的 export_profile 同结构
python tools/roki_cli.py export bundle.json --out exports/
#    会问一次「这些控制是否已在真机上逐个验证过？」，默认 N；
#    非交互场景用 --verified / --unverified 跳过提问

# 5) 直接吃 HA 诊断 JSON（只读草稿，没有 controls）
python tools/roki_cli.py draft diagnostics.json --dc RYYJ --dt R8229
~~~

### 9.4 Issue 反馈

首选 **配置 → 3. 导出贡献包** 生成的 `.md`：五节齐全，profile 与 fixture 都在里面，整份贴到 GitHub Issue（`device_support.yml` 模板）或直接提 PR，维护者用 `tools/promote_profile.py` 落地。

只有诊断 JSON 时也可以附到 Issue，维护者会用 `python tools/roki_cli.py draft <诊断文件>` 生成 profile + fixture 再走同一套流程。

### 9.5 隐私与安全

- 采集与生成草稿的过程只读缓存 / 只发只读轮询，不发送任何控制命令；唯一的写动作是用户手动按下 button 实体。
- 草稿与 fixture 的 properties 在写入前脱敏，重复脱敏幂等。
- 没有学习结果时草稿的 `controls` 段是注释模板；有学习结果时生成 `kind: mqtt` 条目（`verified: false`），重载后按 4.6 显示为 button 实体，且要先打开「允许未验证的控制」才可见、可点。
