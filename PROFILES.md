# YAML profiles（只读遥测）

本文说明 `custom_components/robam_roki/profiles/` 与用户目录里的 YAML profile：它解决什么问题、文件放哪、字段怎么填、冲突时谁赢、日志怎么读。

> P1 阶段 profile **只读**：只会生成 sensor / binary_sensor，永远不会生成 fan / light / number / select，也不会向设备发送任何命令。

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
| `controls` | 否 | 映射 | **P1 预留，不会生效** |

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

### 4.5 controls（P1 预留）

字段与 `devices/base.py` 的 `CommandSpec` 对齐；P1 只解析校验，不会生效（不生成控制实体、不发命令）：

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `kind` | `property` / `invoke` | 必填，否则该控制条目被跳过 |
| `on` / `off` | 映射 | property 的写入负载 |
| `function` | 字符串或整数 | invoke 的协议函数号 |
| `param` | 字符串 | invoke 的参数名 |
| `params` | 映射 | invoke 的额外参数 |
| `verified` | 布尔 | 是否已在真机验证 |

YAML 小坑已由加载器处理：PyYAML 会把裸写的 `on:` / `off:` 解析成布尔键，加载时归一化回 `"on"`/`"off"`；`param: on` 这类布尔标量也会还原成 `"on"`。加不加引号等价。

## 5. 优先级与合并规则

1. **用户 > 内置**；
2. 同一来源内：`dt` 精确匹配 > `dt` 列表包含 > 仅 `dc`；
3. 同精度冲突：记 warning，按文件名排序取第一个；
4. `extends` 合并：按 section（sensors / binary_sensors / controls）浅合并，**同名 key 整条替换**，不递归合并字段；
5. profile 覆盖驱动已有 key 时：只替换你写出的字段。只写 `name`/`icon` 等展示字段 → 取值逻辑保持驱动原样；写了 `keys` → 取值逻辑也由 profile 接管；
6. `extends: python:<name>` 继承内置驱动，可用名称：`hood`、`thermal`、`steam_oven`、`oven`、`microwave`、`sterilizer`、`stove`、`dishwasher`、`water_purifier`、`generic`。

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

与集成自带的 `profiles/thermal-override.yaml` 同构：

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
| `unsupported schema` | `schema` 不是 `roki-profile/1`，整个文件被跳过 |
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

## 8. 安全边界

- profile 生成的一切都是**只读**的：`commands()` 恒为空，因此不会出现控制实体，也不会向设备发送命令。
- 不主动探测设备：取值只读 coordinator 已经缓存的属性。
- `controls` 在 P1 只解析、校验，不会生效；等真机验证后才会进入 P2/P3。
- 未知属性名不会报错，只是取不到值时按 `default`/`None` 处理。

## 9. 自助接入（不用写代码）

拿到 profile 的三条路径，按门槛从低到高：

### 9.1 Home Assistant 服务（推荐）

前提：集成已经添加并正常轮询。草稿只使用**已缓存**的属性，不会为了生成草稿请求云端，也不会向设备发命令。

1. 开发者工具 → 操作 → 选择 `robam_roki.draft_profile`。
2. `device_id` 与 `guid` 二选一（前者从设备注册表选设备，后者直接填 GUID）。
3. 可选 `write_fixture`（默认开启，同时写回归 fixture）。
4. 执行后查看持久通知：先给文件路径与下一步，最后附整份 YAML 全文。

生成位置：

| 文件 | 路径 |
| :--- | :--- |
| profile | `<配置目录>/robam_roki/profiles/draft-<dc>-<dt>.yaml` |
| fixture | `<配置目录>/robam_roki/fixtures/<dc>-<dt>.json` |

同名文件不会被覆盖：再次生成会追加 `-2`、`-3`。

下一步：编辑草稿（把注释建议改成真实字段、按需把布尔属性移入 `binary_sensors`）→ 设置 → 设备与服务 → ROBAM ROKI → 重新加载 → 查看实体。

### 9.2 命令行（可选）

需要本机 Python 与依赖：

~~~bash
# 1) 采集：只发只读轮询 130，绝不发控制命令
python tools/roki_cli.py collect --device <GUID> --mark
#    --duration 默认 120s 被动采样；--mark 交互打点（回车结束）
#    产物 bundle.json 的 properties/observations 已脱敏；
#    帧体 hex 不含发送方 guid

# 2) 生成草稿
python tools/roki_cli.py draft bundle.json --out profiles/

# 也可以直接吃 HA 诊断 JSON
python tools/roki_cli.py draft diagnostics.json --dc RYYJ --dt R8229
~~~

### 9.3 Issue 反馈

只有诊断 JSON 时也可以直接附到 GitHub Issue（`device_support.yml` 模板）。维护者会用 `draft` 生成 profile + fixture 并加入本仓库。

### 9.4 隐私与安全

- 服务与 CLI 都只读缓存 / 只发只读轮询，不发送任何控制命令。
- 草稿与 fixture 的 properties 在写入前脱敏，重复脱敏幂等。
- 生成的 `controls` 段全部是注释；当前版本不会生效。
