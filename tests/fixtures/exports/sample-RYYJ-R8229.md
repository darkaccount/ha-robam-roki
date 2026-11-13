# ROBAM ROKI profile export — RYYJ-R8229

> 已脱敏：不含设备 GUID、手机号、token、App GUID

- 生成时间：2026-11-13 02:30
- 集成版本：0.5.0
- 导出格式：roki-export/1
- 设备名称：厨房油烟机

## 1. 设备与匹配

| 字段 | 值 |
| :--- | :--- |
| 设备 | 厨房油烟机 |
| dc | RYYJ |
| dt | R8229 |
| 通道 | MQTT |
| extends | python:hood |

## 2. 验证清单

| key | 名称 | 命令 | body | 观察到的变化 | verified |
| :--- | :--- | :--- | :--- | :--- | :--- |
| action_1 | 开机 | 132 | 01 | workStatus 0 → 1 | false |

验证方法：在集成选项里打开「允许未验证的控制」，重载后逐个按下
这些 button 实体，确认它们和官方 App 上的按钮效果一致；确认后
把 profile 里对应的 verified 改成 true。

## 3. profile YAML

<!-- roki:profile-yaml -->
```yaml
schema: roki-profile/1
extends: python:hood
match:
  dc: RYYJ
  dt: R8229
controls:
  action_1:
    # 名称是按设备变化推测的，建议改成 App 上显示的名字
    name: "开机"
    kind: mqtt
    command: 132
    body_hex: "01"
    verified: false
```
<!-- /roki:profile-yaml -->

## 4. fixture

- 本机路径：`/config/robam_roki/fixtures/RYYJ-R8229.json`
- 目标仓库路径：`tests/fixtures/devices/RYYJ-R8229.json`

<!-- roki:fixture-json -->
```json
{
  "schema": "roki-fixture/1",
  "source": "auto",
  "match": {
    "dc": "RYYJ",
    "dt": "R8229"
  },
  "properties": {
    "workStatus": 1,
    "level": 2,
    "phone": "**REDACTED**"
  },
  "expect": {
    "work_status": "开机",
    "fault_code": null
  }
}
```
<!-- /roki:fixture-json -->

## 5. 如何贡献

1. 到 GitHub 提 issue（模板「新设备支持」）：https://github.com/darkaccount/ha-robam-roki/issues/new?template=device_support.yml
   把本文件整份贴进去即可，profile 与 fixture 都在里面。
2. 或者直接提 PR：维护者（或你自己）运行
   `python tools/promote_profile.py <本文件>`，它会按第 3、4 节把
   profile 与 fixture 落到 custom_components/robam_roki/profiles/ 与 tests/fixtures/devices/。
3. 仓库地址：https://github.com/darkaccount/ha-robam-roki
