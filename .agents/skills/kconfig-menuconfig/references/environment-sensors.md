# Environment sensors: covering every Klipper I2C `sensor_type` (plan and status)

Status as of 2026-10-03: every phase (#1305–#1314) and the follow-ups (#1315) are merged into `development`. Every Klipper I2C environment `sensor_type` can now be chosen in menuconfig, for both a shared sensor and per-gate sensors.

---

## Status

### What shipped

| Phase | PR | State | What landed | Different from the plan |
|---|---|---|---|---|
| — | #1306 `feat(kconfig): extend range with multiple bound pairs` | merged | `range 0 0 5 300` syntax: exactly 0, or 5–300 | Not in the plan. It replaced the planned `W31` warning (see phase 0). |
| 0 | #1305 `fix(environment): honour report time 0 and reject values below Klipper's minimum` | merged | The template guard compares the report time as an int, so 0 leaves the option out. Report time uses `range 0 0 5 300` for every type. The report-time option name is explicit per type, with a `""` fallback. Help text drops "patched aht1x driver" and states the firmware versions. Golden, parity and range tests added. | The `W31` warning was built, then dropped in favour of the #1306 range, which rejects 1–4 in menuconfig itself. The `i2c_address` fallback of 56 was kept: an int symbol has no empty value, and the test table pins each type's address. |
| 1 | #1301 `feat(environment): support HTU21D environment sensors` | merged before phase 0 | HTU21D type | Its `range 5 300` became `0 0 5 300` in #1305. |
| 2 | #1308 `feat(environment): add AHT10 legacy sensor type` | merged | AHT10, listed first, for Klipper ≤ v0.13.0 and Kalico before v2026.06 | The optional install-time firmware probe (`$(klipper-sensor-type,…)` plus a warning) was not built. |
| 3 | #1309 `feat(environment): support SHT3X sensors` | merged | SHT3X, address 68, `sht3x_report_time` with its own `range 0 300` ahead of the general line | — |
| 4 | #1310 `feat(environment): add SI7013/SI7020/SI7021/SHT21 sensor types` | merged | Four flat choice members using the HTU21D driver | The per-gate test now sets the gate count to the number of types. |
| 5 | #1311 `feat(environment): support LM75 temperature-only sensors` | merged | LM75, address 72, `lm75_report_time` sharing SHT3X's `range 0 300`. Help says drying ignores the humidity goal. | The optional runtime warning and the `expectedFailure` test were not done (see follow-ups). |
| 6 | #1312 `chore(environment): clarify BME280 chips and tidy sensor type labels` | merged | BME280 label lists the BMx80 chips; help says which report humidity; the address help notes 119 (0x77). All qualifier labels use `Main [[DIM]]/ qualifier[[/DIM]]`. | No BMx80 chip members (as planned). The labels follow the NFC reader's `[[DIM]]` pattern rather than brackets. |
| 7 | #1313 `feat(environment): expose HTU21D resolution and hold-master options` | merged | For the HTU21D family only: a resolution choice whose default "Klipper default" writes nothing, and a hold-master `boolint` written only when on. The template guards both on the family. | The plan said "render only when it differs from TEMP12_HUM08". It became an explicit "Klipper default" choice member instead, so picking TEMP12_HUM08 still writes it. |
| 8 | #1314 `feat(kconfig): allow source inside @repeat and share environment sensor definitions` | merged | Fork fix so `source` works inside `@repeat` (`_line_queue_stack`), plus `components/Kconfig.environment_sensor_type` and `_address`, sourced once for the shared sensor and once per gate | Used option (iii) then (ii). Parse-tree dumps of four parse shapes are identical except help text: per-gate types and address gain the shared help, and the type-choice help reads "Select the type of environment sensor". |

### Decisions taken

1. **Report time below Klipper's minimum:** first a `W31` warning with `range 0 300`, then replaced by `range 0 0 5 300` from #1306. 0 means "leave the option out".
2. **HTU21D family:** flat choice members.
3. **Install-time firmware probe:** not built; help text only.
4. **Temperature-only types:** offered (LM75, and BMP chips under BME280), with help text explaining that drying ignores the humidity goal.
5. **Choice labels:** never brackets. Use `Main [[DIM]]/ qualifier[[/DIM]]`.

### Final per-type behaviour (`development` plus #1313)

| Members | `sensor_type` | Report option | Report-time range | Default address | Extra options |
|---|---|---|---|---|---|
| AHT10, AHT1X, AHT2X (default), AHT3X | same name | `aht10_report_time` | `0 0 5 300` | 56 (0x38) | — |
| BME280 / BMP180 / BMP280 / BMP388 / BME680 | `BME280` | none (prompt hidden) | — | 118 (0x76); help notes 119 (0x77) | — |
| HTU21D, SI7013, SI7020, SI7021, SHT21 | same name | `htu21d_report_time` | `0 0 5 300` | 64 (0x40) | resolution, hold master (#1313) |
| SHT3X | `SHT3X` | `sht3x_report_time` | `0 300` | 68 (0x44) | — |
| LM75 | `LM75` | `lm75_report_time` | `0 300` | 72 (0x48) | — |

In every case, 0 leaves the report option out so Klipper uses its own default: 30 s for AHT and the HTU21D family, 1 s for SHT3X, 0.8 s for LM75.

Tests live in `TestEnvironmentSensorReportTime` (`test/test_mmu_config.py`):
- `SENSOR_TYPES` table: each of the 12 types renders the expected `sensor_type`, `i2c_address` and report option, shared and per-gate.
- The shared and per-gate Kconfig files offer the same set of types.
- Report time 0 leaves the option out.
- Accepted and rejected report times per type, checked through kconfiglib's `str_value`, because the build path bypasses `range`.
- (#1313) HTU21D options render only when chosen, and only for the HTU21D family.

The per-gate type test puts one type on each gate and now uses all 12 gates, the limit. A 13th type would need the test split into two parses.

### Follow-ups (#1315)

One PR, `fix(environment): handle missing humidity readings and tidy sensor help`:
- **Read failure ends drying early: fixed.** A chip status of exactly 0 °C and 0 % now counts as no humidity reading, so drying runs to its timer. A real 0 % still meets the goal.
- **Runtime warning: added.** `MMU_HEATER DRY=1` with a humidity goal warns when a sensor reports no humidity (LM75, BMP chips, a failed sensor).
- **`update_aht10_commands`:** help text and `mmu.cfg` comment only; no runtime warning, by decision.
- **HTU21D zero-length i2c note:** added to the sensor type help.
- **"(s)" prompts:** reworded without brackets.
- **Skill doc:** corrected; named choices can gain members from another file.
- **Install-time firmware probe:** not done, by decision.

Tests: new `test/test_mmu_drying.py`, the first test that runs a drying cycle.

### Process notes

- **Stacked PRs:** phases 3–7 were stacked because they edit the same lines. A stacked PR gets only the title check until it's retargeted to `development`, and retargeting doesn't start CI.
- **Squash-merged base:** #1308 and #1309 were squash-merged, so `gh pr update-branch` failed with conflicts on the next PR. The fix was `git rebase --onto origin/development <old base tip>`, then the same for each later PR, checking the trees were unchanged, then `push --force-with-lease`.
- **Merge-commit base:** #1310–#1312 were merged with merge commits, so `gh pr update-branch` worked.

---

The sections below are the original research and plan. Line numbers marked [HH] refer to `origin/development` `37e66778` at planning time. The status above supersedes §3b's warning rule, §5 and §6.

**Sources.** Each citation names the checkout it came from. Line numbers differ between checkouts.

| Key | Checkout | Commit |
|---|---|---|
| **[K]** | `~/github/klipper`, mainline | `d86599740` (2026-07-31) |
| **[K-old]** | `~/klipper` | `ed36041b6` (2025-05-13) = `v0.13.0-111`, before the AHT rework at `v0.13.0-413` |
| **[KC]** | `~/github/kalico` | `e2d49914`. The sensor files and `temperature_sensors.cfg` are identical to `origin/main` `ae261624` (2026-08-05) |
| **[HH]** | Happy Hare | `origin/development` `37e66778`, unless a cite says #1301 |

All Klipper paths are under `klippy/extras/`.

---

## 0. Headline findings

1. **The default member `AHT2X` does not boot on any tagged Klipper release.** *(Addressed by #1308: AHT10 option plus help text.)*
   - `AHT1X`, `AHT2X` and `AHT3X` reached Klipper in `1f43be0b8` on 2025-11-15, which `git describe` places at `v0.13.0-413` ([K] `aht10.py:199-203`).
   - v0.13.0 is still the newest upstream Klipper tag (checked with `git ls-remote` against Klipper3d/klipper).
   - [K-old] registers only `AHT10` (`aht10.py:162`).
   - Kalico added the new names in `de7e90be`. Upstream KalicoCrew v2026.06.00 is the first tag that contains it; v2026.05.00 is one commit behind it (GitHub compare API).
   - On older firmware Klipper refuses to start with `Unknown temperature sensor 'AHT2X'` ([K-old] `heaters.py:294-296`; the same code is at [K] `heaters.py:297-299`).
   - [HH] makes `AHT2X` the default (`Kconfig.environment_sensor:93`, `Kconfig.per_gate:44`).
2. **The "0 = disable" report time doesn't work, and neither do values 1–4.** *(Fixed by #1305.)*
   - The template guard was `[% if PARAM_SENSOR_REPORT_TIME_PARAM and PARAM_ENVIRONMENT_SENSOR_REPORT_TIME %]` ([HH] `config/base/mmu_hardware.cfg:595,614`). INT symbols reach Jinja as strings, and `"0"` is truthy.
   - I rendered both cases through the real harness:
     - per-gate `REPORT_TIME_0=0` → `aht10_report_time: 0`;
     - per-gate `REPORT_TIME_0=3` → `aht10_report_time: 3`.
   - Klipper requires `minval=5` ([K] `aht10.py:40`), so both settings fail at boot.
   - [HH] allowed these values: the shared range was `1 300` (`Kconfig.environment_sensor:135`) and the per-gate range `0 300` (`Kconfig.per_gate:78`). The help text promised "0 to disable" (`:141`, `:84`).
   - The broken guard and the per-gate `0 300` range both arrived in `c5762a002` (2026-09-06).
3. **The environment manager already reads humidity from every Klipper I2C module.**
   - `ENV_SENSOR_CHIPS = ["bme280","htu21d","sht3x","lm75","aht10"]` ([HH] `extras/mmu/unit/mmu_environment_manager.py:51`). Each Klipper module registers its object under exactly that prefix: [K] `aht10.py:44`, `bme280.py:155`, `htu21d.py:106`, `sht3x.py:64`, `lm75.py:36`.
   - So adding sensor types was a Kconfig and template job only. No runtime change was needed in any phase.
4. **`update_aht10_commands` does nothing on current Klipper and Kalico.** *(Still open.)*
   - It assigns `aht10.AHT10_COMMANDS` ([HH] `extras/mmu/mmu_controller.py:127-136`). That dict exists only in [K-old] `aht10.py:18`.
   - [K] and [KC] use `CMD_INIT_AHT1X`/`CMD_INIT_AHT2X` constants instead ([K] `aht10.py:22-23`, [KC] `aht10.py:24-25`), so the assignment silently has no effect.

---

## 1. Coverage matrix

Every `temperature_sensor` `sensor_type` registered by an I2C module, from `add_sensor_factory` calls grepped in all three checkouts. `sensor_type` matching is exact and case-sensitive ([K] `heaters.py:273-274,296-300`).

| `sensor_type` | Module | Default addr (code) | Report-time option (Klipper default, min) | Other module options | `get_status` keys | [K] | [K-old] | [KC] | HH at planning | HH now |
|---|---|---|---|---|---|---|---|---|---|---|
| `AHT10` | aht10.py | 0x38 = 56 | `aht10_report_time` int (30, ≥5) | none | temperature, **humidity** | ✔ alias of AHT1x `:199` | ✔ only name `:162` | ✔ `:230` | ✗ | ✔ #1308 |
| `AHT1X` | aht10.py | 0x38 = 56 | same | none | temperature, **humidity** | ✔ `:201` | ✗ | ✔ `:232` | ✔ | ✔ |
| `AHT2X` | aht10.py | 0x38 = 56 | same | none | temperature, **humidity** | ✔ `:202` | ✗ | ✔ `:233` | ✔ (default) | ✔ (default) |
| `AHT3X` | aht10.py | 0x38 = 56 | same | none | temperature, **humidity** | ✔ `:203` | ✗ | ✔ `:234` | ✔ | ✔ |
| `BME280` (chip autodetected: BMP180, BMP280, BME280, BMP388, BME680) | bme280.py | 0x76 = 118 | **none**: fixed `REPORT_TIME = .8` | `bme280_iir_filter`, `bme280_oversample_{temp,hum,pressure}`, `bme280_gas_target_temp`, `bme280_gas_heat_duration` | temperature, pressure; **humidity only on BME280/BME680**; gas on BME680 | ✔ `:796` | ✔ `:804` | ✔ `:891` | ✔ | ✔ (relabelled, #1312) |
| `HTU21D` | htu21d.py | 0x40 = 64 | `htu21d_report_time` int (30, ≥5) | `htu21d_hold_master` (False), `htu21d_resolution` | temperature, **humidity** | ✔ `:255-256` | ✔ `:250` | ✔ `:284-285` | ✗ (#1301 open) | ✔ #1301; options in #1313 |
| `SI7013`, `SI7020`, `SI7021`, `SHT21` | htu21d.py | 0x40 = 64 | same | same | temperature, **humidity** | ✔ | ✔ | ✔ | ✗ | ✔ #1310; options in #1313 |
| `SHT3X` | sht3x.py | 0x44 = 68 | `sht3x_report_time` int (**1**, ≥1) | none | temperature, **humidity** (rounded to 0.1) | ✔ `:181` | ✔ `:174` | ✔ `:200` | ✗ | ✔ #1309 |
| `LM75` | lm75.py | 0x48 = 72 | `lm75_report_time` **float** (0.8, ≥0.5) | none | temperature **only** | ✔ `:108` | ✔ `:108` | ✔ `:121` | ✗ | ✔ #1311 |

<details>
<summary>Citations for the matrix</summary>

**AHT family**
- [K] `aht10.py`: address `:18`; report time `:40`; status `:167-171`.
- [K-old] `aht10.py`: address `:16`; report time `:33`; status `:152-155`.
- [KC] `aht10.py`: address `:20`; report time `:45`; status `:194-197`.
- Init sequences, [K] `aht10.py`: AHT1x sends `0xE1` (`:22,173-178`), AHT2x sends `0xBE` (`:23,180-185`), and AHT3x waits for power-on calibration (`:187-192`).

**BME280 / BMx80**
- Address `bme280.py:10`; report time `:9`.
- Options `:137-142`.
- Chip map `:94-97`. [KC] `:124-130` has the same five chips.
- Chip detection `:279-283`. Before detection the chip type defaults to `'BMP280'`, which reports no humidity (`:153`).
- Status `:781-789`.
- The second address 0x77 (= 119) for BMP180, BMP388 and some BME280 boards comes **from the docs only** ([K] `docs/Config_Reference.md:3018-3019`). The code default is 0x76 for every chip.

**HTU21D family**
- [K] `htu21d.py`: address `:22`; options `:95-99`.
- Resolution choices are `TEMP14_HUM12`, `TEMP13_HUM10`, `TEMP12_HUM08` and `TEMP11_HUM11` (`:38-43`).
- **The code default is `TEMP12_HUM08` (`:98`), not the `TEMP11_HUM11` the docs give.** [KC] `:103` agrees with the code.
- Conversion wait times are keyed by the *configured* `sensor_type` (`:57-83`, used at `:169,193`). A chip-ID mismatch only logs "Forcing to type … as config" (`:140-149`).
- The driver needs MCU support for zero-length i2c writes (`:10-11`).
- Status `:245-249`.

**SHT3X**: [K] `sht3x.py` address `:15`; report time `:60`; status `:172-176`.

**LM75**: [K] `lm75.py` address `:9`; report time `:18,22,32-33`; status `:99-101`.

**Common to every module**: `bus.MCU_I2C_from_config` ([K] `bus.py:302-326`) reads:
- `i2c_mcu` (`:306`);
- `i2c_speed`, default 100000 and **minval 100000** (`:307`);
- `i2c_address`, 0–127 (`:309-311`);
- either `i2c_bus` or `i2c_software_scl_pin`/`sda_pin`. Software pins must be on `i2c_mcu` (`:314-320`).

</details>

**Not covered, deliberately:**
- `DS18B20` is 1-wire, not I2C. It needs `serial_no` and `sensor_mcu`, and its report option is `ds18_report_time` ([K] `ds18b20.py:19,22-27,81`). It doesn't fit an I2C bus/address screen; if wanted, it belongs in a separate follow-up.
- `temperature_mcu`, `temperature_host`, `temperature_combined`, thermistors and SPI sensors are not I2C environment sensors.
- [KC]'s extra types (`mpc_*`, `indx`) are not environment sensors.
- **SHT4x, HDC1080 and SHT2x-other do not exist** in any of the three checkouts.

**Version differences that matter:** only the AHT names (row 1 vs rows 2–4). The option names and defaults of `bme280`, `htu21d` and `sht3x` are unchanged between [K-old] and [K]. The diffs are internal: CRC handling, `i2c_write_wait_ack` removal, retries.

---

## 2. Gaps that matter to Happy Hare

**How the manager behaves when a sensor has no humidity.**
- `_get_environment_status` returns `(temp, None)` ([HH] `mmu_environment_manager.py:848-880`).
- The humidity-goal checks are skipped when the value is `None` (`:481`, `:529`). Drying runs to its timer and finishes with "Final humidity: unknown" (`:519-522`).
- `MMU_HEATER DRY=1` only requires that *a* sensor is configured (`commands/mmu_heater.py:176-178`).
- `HUMIDITY=` always has a value, because it falls back to `heater_default_dry_humidity` (`:102`). For a sensor without humidity that goal is **silently ignored**.

**Temperature-only types are offered** (LM75 in #1311, BMP chips under BME280 in #1312), and their help says drying ignores the humidity goal. The optional runtime warning is still a follow-up.

**Side bug, not fixed: a sensor read failure ends drying as if the goal were reached.**
- On a read failure every module sets `humidity = 0` and stops sampling: [K] `aht10.py:143-145`, `htu21d.py:223`, `sht3x.py:145-146`, `bme280.py:545-546`.
- HH then sees `0 <= target` and ends drying with "humidity goal reached".
- This is a good `@unittest.expectedFailure` candidate.

---

## 3. Kconfig design

### 3a. Choice shape

| Option | Shape | Verdict |
|---|---|---|
| **A. Flat: one member per `sensor_type`** | Keep `CHOICE_ENVIRONMENT_SENSOR_TYPE[_$(i)]` and add members, each mapping 1:1 to a Klipper string. | **Chosen.** Purely additive, no symbol renames. The list is now 12 entries, with qualifiers dimmed, e.g. `SI7021 [[DIM]]/ HTU21D family[[/DIM]]`. |
| B. Family plus variant | `FAMILY ∈ {AHT, BMx80, HTU21D, SHT3X, LM75}` plus a per-family `VARIANT` choice. | **Rejected.** Saved configs hold `CHOICE_…_AHT2X=y`. Moving that to `FAMILY_AHT + VARIANT_AHT2X` is one symbol becoming two. `HH_RENAMED_SYMBOLS` maps one old name to one new name ([HH] `installer/lib/kconfiglib/kconfiglib.py:601`), so it can't express the split. |
| A′. Flat plus one variant sub-choice | The HTU21D family as one member with a `CHOICE_…_HTU21D_VARIANT` sub-choice. | Not used. |

**Per-gate cost.** Each new member adds 12 symbol nodes to every parse (the skill's pitfall 10). The 7 new members added 84 nodes per parse, and #1313 adds about 8 per gate. In multi-unit setups that cost is paid once per unit.

### 3b. How the derived values follow the type

The final values are in the status table above. Design rules as built:
- **No fallthrough default that names a real option.** `PARAM_SENSOR_REPORT_TIME_PARAM[_n]` names each type's option explicitly and falls back to `""` (#1305). The address still falls back to 56, and the test table pins every type's address.
- **"0" means "omit the option, use Klipper's default".** The guard is `|int > 0` at `mmu_hardware.cfg:595,614` (#1305).
- **Below-minimum values are rejected in menuconfig** with `range 0 0 5 300` (#1306 syntax), not warned about. An out-of-range saved value falls back to the default of 60 (`kconfiglib.py` int range handling). Every such value already failed at Klipper boot, so no working setup changed.
- **Per-type minimums:** the first active `range` line wins, so SHT3X and LM75 get `range 0 300 if …` ahead of the general line.
- **The address default follows the type** through `default N if CHOICE_…_X`. A user-set address stays put across type changes.

### 3c. Optional per-type options

| Option | Outcome |
|---|---|
| `i2c_speed` | Not exposed. Klipper's minimum and default are both 100000 ([K] `bus.py:307`). |
| `htu21d_resolution` | #1313: a choice whose default "Klipper default" writes nothing, so Klipper uses `TEMP12_HUM08`. |
| `htu21d_hold_master` | #1313: a `boolint` written only when on. |
| `bme280_*` oversampling, IIR and gas | Not exposed. |
| BMx80 chip members | Not added; the label `BME280 [[DIM]]/ BMP180 / BMP280 / BMP388 / BME680[[/DIM]]` covers them (#1312). |

**Template guard for options that only one driver reads.** The build reads saved values of hidden symbols (`build.KConfig` raw-value fallback), and Klipper rejects an option a section's driver doesn't read. So #1313 writes the HTU21D options only when the report option is `htu21d_report_time`. With the guard removed, a test with leftover values on an AHT sensor renders `htu21d_hold_master: 1`.

### 3d. Keeping the shared and per-gate definitions in step

| Option | Status |
|---|---|
| **(i) Parity test** | Done (#1305): the type table renders shared and per-gate, and a test checks both files offer the same members. |
| (ii) Shared component under `installer/components/` | Done in phase 8: `Kconfig.environment_sensor_type` (type, derived values, report time, HTU21D options) and `Kconfig.environment_sensor_address`, sourced at the same menu positions as before. Later merged into one `Kconfig.environment_sensor` that also holds the i2c bus type, bus name, address and pins, after the shared sensor's prompts were reordered to match per-gate; the shared sensor's fixed bus list is a re-opened named choice in its caller, and per-gate's "Custom bus name" an optional prompt (`env_sensor_custom_bus`). Before phase 8, **a `source` inside `@repeat` was not supported.** The repeat body is queued on one parser-global `_line_queue`, which is drained before any file read (`kconfiglib.py:2447-2448,4000-4011`), and nothing in `_enter_file` saves or restores it. So a component means either 13 hand-unrolled `suffix := … / source` pairs, or option (iii). |
| (iii) Fork fix: save and restore `_line_queue` across `source` | Done in phase 8 (`_enter_file`/`_leave_file`, `_line_queue_stack`), tested by `test/installer/test_kconfig_repeat_source.py`. It would unlock components in per-gate blocks for every feature (NFC, heaters too). It's a fork change, so it needs `make verify_pickle`, `test_menuconfig.py` and a spike test. |

---

## 4. Compatibility

- **No phase renamed or removed a member, so no `HH_RENAMED_SYMBOLS` entries were needed.**
- **The default stays `AHT2X`.** Choices saved with `#~DEFAULT~#` are recomputed on every load, so changing the default would silently switch every user who never touched it.
  - **Never auto-map `AHT2X` to `AHT10`.** On [K] and [KC], `AHT10` is AHT1x with the `0xE1` init, not AHT2x's `0xBE` ([K] `aht10.py:199,176-185`).
- **Optional install-time warning (not built).** A `kconfigfunctions.py` helper, e.g. `$(klipper-sensor-type,AHT2X)`, could grep `$(real_klipper_home)/klippy/extras/*.py` for `add_sensor_factory("AHT2X"`.
  - `real_klipper_home` exists (`installer/Kconfig:180`). The detection #1221 added is runtime-only (`is_kalico`, `mmu_controller.py:78`).
  - When the tree is missing or unreadable (CI, the test harness, a non-standard Klipper location), the helper must return "unknown" and stay silent.
- **Out of scope but flagged: `update_aht10_commands`** ([HH] `Kconfig.options:422-431`). Its help says to use `AHT10` with this flag on older Klipper. After a Klipper upgrade the flag does nothing (§0.4), so a ViViD user still on `AHT10` plus the flag quietly gets the AHT1x `0xE1` init on an AHT30. The ViViD custom setup ships `AHT3X` (`installer/boards/custom/Kconfig.vvd:85-96`), so only users who followed the fallback advice are affected.
- **Test-harness caveats:**
  - The fake Klipper doesn't validate `sensor_type` or `minval`, so tests assert on rendered text against the §1 table.
  - `build.KConfig.get` reads raw user values and so **bypasses `range`**. Range behaviour is pinned through kconfiglib's `Symbol.str_value`, not through `cfg.render`.
  - The per-gate harness parse (`MMU_TYPE_EMU_1_0` with a per-gate MCU) accepts `PARAM_NUM_GATES` up to 12.

---

**Aside for the skill doc.** The `kconfig-menuconfig` skill says "`choice` members cannot come from another file". In fact `boards/per_gate/Kconfig.slb:142-147` adds members to the named choice `CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_$(gate)` from a board file. Named choices merge in this fork, so that skill line is worth correcting.
