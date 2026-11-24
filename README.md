# proj_3006_SimpleHeatmap

สองงานแยกกัน ใช้ config และ log ร่วมกัน

| งาน | ไฟล์ | Task | เวลา | ทำอะไร |
|---|---|---|---|---|
| เก็บราคา | `simple_heatmap.py` | `SimpleHeatmap` | 10:00, 21:00 จ.–ศ. | MT5 bid/ask → append ลง tab `Data` ของ Google Sheet |
| Rollover | `rollover.py` | `SimpleHeatmap_Rollover` | 10:00 จ.–ศ. | Airtable → ภาพ matrix ต้นทุน roll COMEX → Google Chat |

`chat.py` คือตัวส่งข้อความ/รูปเข้า Google Chat ที่ `rollover.py` ใช้

> ตั้งแต่ 2026-11-24 เลิกทำ heatmap แล้ว (tab `Front`/`Back`/`Back_series`/`Swap` ในชีตยังอยู่
> แต่ไม่มีโค้ดดูแล) เหลือเก็บราคาไว้ใน `Data` อย่างเดียว โค้ดเก่าดูได้ใน git history ก่อน commit นี้

Sheet: `1MmAn7aVFIdReaMZgxoN-thw-LMtBFna4B5qCKI8kNpU`

---

## 1. เก็บราคา — `simple_heatmap.py`

### Sheet layout (worksheet `Data`)

|       | A          | B        | C        | D       | E        | F       |
|-------|------------|----------|----------|---------|----------|---------|
| แถว 1 | Date       | Time     | XAUUSD.s |         | CMX.GCV6 |         |
| แถว 2 |            |          | BID      | ASK     | BID      | ASK     |
| แถว 3 | 2026-09-04 | 10:00:01 | 4474.12  | 4474.60 | 4485.50  | 4485.90 |

- **แถว 1 คือ config** — คอลัมน์แรก ๆ ที่ชื่อ `Date` / `Time` / `Timestamp` คือบล็อกเวลา
  ส่วนที่เหลือคือชื่อ symbol โดยแต่ละตัวกิน 2 คอลัมน์ (BID, ASK)
  เพิ่ม/ลด/สลับ symbol ทำที่ sheet อย่างเดียว ไม่ต้องแก้โค้ด
- แถว 2 (BID/ASK) สคริปต์เขียนให้เองอัตโนมัติถ้ายังไม่มี ข้อมูลเริ่มที่แถว 3 เสมอ
- **Timestamp คือเวลา snapshot ที่ตั้งไว้ ไม่ใช่เวลาที่รันสคริปต์** — ดึง tick ตัวสุดท้าย
  ณ หรือก่อนเวลานั้นพอดีผ่าน `copy_ticks_range` รันกี่โมงก็ได้ผลเท่าเดิม และย้อนหลังได้
- ตัวกันเขียนซ้ำใช้คู่ `(วันที่, เวลา)` วันละ 2 แถว
- Timestamp เป็น **เวลาไทย (GMT+7)**; MT5 เดินด้วย GMT+0 — เวลาของ tick จริง (UTC) อยู่ใน log
- symbol ไหนดึงราคาไม่ได้จะเว้นเซลล์ว่างไว้ (ไม่ใส่ 0)

### ⚠️ เวลา contract หมดอายุ ให้ append คอลัมน์ใหม่ ห้ามแทนที่ของเดิม

ชื่อและ TTM อยู่ในแถว 1 ที่ทุกแถวใช้ร่วมกัน ถ้าเอา contract ใหม่ไปทับคอลัมน์เดิม
จะไม่เหลือร่องรอยว่าราคาก่อนหน้าเป็นของ contract ตัวไหน ให้ปล่อยตัวเก่าค้างไว้
(ช่องจะว่างเองหลังหมดอายุ) แล้วเติมตัวใหม่ต่อขวาสุด

**เพิ่ม contract ล่วงหน้าก่อนโบรกเกอร์ list ได้** — ใส่ชื่อกับ TTM ใน `Data` แล้วใส่ชื่อใน
`unquoted_symbols` ของ `config.json` job จะไม่ดึงราคาจนกว่าจะเอาชื่อออก
แก้ชื่อทับได้เฉพาะคอลัมน์ที่ยังไม่มีข้อมูล (เช่น `CMX.GCM7` → `CMX.GCEM27` เมื่อ 28 ต.ค. 2026)

### การใช้งาน

```bash
uv sync
```

```bash
uv run python simple_heatmap.py --dry-run
```

```bash
uv run python simple_heatmap.py
```

```bash
uv run python simple_heatmap.py --date 2026-09-03
```

### กรณีข้ามวัน (เครื่องปิด / task ไม่ได้รัน / MT5 ไม่เปิด)

**ไม่ต้องทำอะไร — รอบถัดไปเก็บย้อนให้เอง** สคริปต์ไล่เก็บทุก slot ที่ยังขาดจนถึงตอนนี้

- วันที่และเวลาที่มีอยู่แล้วจะข้าม รันมือแทรกระหว่างวันได้ปลอดภัย
- เสาร์–อาทิตย์ข้ามตามปฏิทิน วันหยุดตลาดข้ามเพราะไม่มี tick ไม่เขียนแถวว่าง
- `max_backfill_days` (ค่าเริ่มต้น 10) จำกัดว่าย้อนได้ไกลสุดกี่วัน
- `--date` เขียนต่อท้ายเสมอ ถ้าใช้กับวันเก่ากว่าแถวล่าสุด ลำดับแถวจะไม่เรียงตามวันที่

ตั้ง scheduled task:

```bash
schtasks /create /xml SimpleHeatmap_task.xml /tn SimpleHeatmap
```

task เรียก `.venv\Scripts\pythonw.exe` ตรง ๆ เพื่อไม่ให้มีหน้าต่างเด้ง ผลคือ lockfile ไม่ถูกตรวจตอนรัน
(แก้ dependency แล้วต้อง `uv sync` เอง) และ `sys.stdout`/`sys.stderr` เป็น `None` —
`setup_logger` จึงเพิ่ม console handler เฉพาะตอนมี stdout

---

## 2. Rollover — `rollover.py`

ตอบคำถามว่า "roll จากขาหนึ่งไปอีกขาเสียกี่ %/ปี" สำหรับทุกคู่ของ COMEX GC ที่มีราคา
ข้อมูลมาจาก Airtable base `appXoUtj6dYJesMRG`: table `Contracts` (วันหมดอายุ + checkbox
`Active Month`) และ `Outright Quotes` (bid/ask รายวัน)

### ภาพ

- **matrix สามเหลี่ยมบน** — แถว = ขาใกล้, คอลัมน์ = ขาไกล; หัวแถว/คอลัมน์แสดง ask (แดง) บน bid (เขียว)
- **ในช่อง:** ป้าย `Z26-F27 (29d)` → `spread (yield%)` → บรรทัดจาง = spread ที่ได้ recommend yield พอดี

```
spread = far ask − near bid                        ขายขาใกล้ที่ bid ซื้อขาไกลที่ ask
yield% = spread / near bid × 360 / (far expiry − near expiry)
spread @ recommend = near bid × recommend% × days / 360
```

- **10 ช่องที่ yield ต่ำสุด** ระบายเขียว เข้มสุดที่อันดับ 1 แล้วจางลงตามอันดับ
- **ขีดเขียวหน้าชื่อ** = ขาที่ถืออยู่ตอนนี้; ขีดส้ม = `Stale Flag` (แสดงเฉพาะตอนมี)
- **recommend yield** = `roll_hurdle_pct` ใน `config.json` (ตอนนี้ 4.80%)

### ข้อควรรู้

- **หารด้วยระยะระหว่างวันหมดอายุสองขา** — spread คือ carry ของช่วง near→far เท่านั้น
- **`Bid` กับ `Ask` ใน `Outright Quotes` สลับกัน** (Bid > Ask ทุกแถว) สคริปต์อ่านสลับให้ใน
  `fetch_latest_quotes` ถ้าแก้ table ให้ถูกเมื่อไร ต้องแก้บรรทัดนั้น
- **ขาที่ถืออยู่เลือกเอง** = contract ที่ติ๊ก `Active Month` ตัวแรกที่ FND ยังไม่ถึง
  (FND ≈ วันทำการสุดท้ายของเดือนก่อนเดือนส่งมอบ) ดูแบบอื่นชั่วคราวด้วย `--from G27`
- **matrix แสดงทุก contract ที่มีราคาใน snapshot** ตั้งแต่ขาที่ถืออยู่เป็นต้นไป
- **ไม่ส่งซ้ำ** — จำ `(snapshot, ขาที่ถือ)` ที่ส่งล่าสุดไว้ใน `logs/rollover_last.json`
  (`--space` ไม่บันทึก จึงส่งเข้า TestSpace ได้โดยไม่กินรอบของ space จริง)
- Airtable อัปเดตราว 14:00–14:30 รูปตอน 10:00 จึงเป็นข้อมูลของบ่ายวันก่อน (เวลา last record อยู่มุมขวาบน)
- รูปล่าสุดเขียนทับที่ `logs/rollover.png`

### โครงโค้ด (อ่านจากบนลงล่าง)

| ส่วน | ฟังก์ชัน |
|---|---|
| Model | `Contract`, `Quote`, `Roll` (spread / yield_pct / spread_at) |
| 1. Read | `load_token`, `fetch_contracts`, `fetch_latest_quotes` |
| 2. Choose | `approx_fnd`, `pick_near` |
| 3. Compute (pure) | `make_roll`, `matrix_legs`, `build_matrix`, `cheapest` |
| 4. Draw | `render` → `_draw_title`, `_draw_column_heads`, `_draw_row_head`, `_draw_cell`, `_draw_footer` |
| 5. Publish | `main` (+ `_log_near_rolls`, `_last_sent`, `_mark_sent`) |

### การใช้งาน

```bash
uv run python rollover.py --dry-run
```

```bash
uv run python rollover.py --out roll.png
```

```bash
uv run python rollover.py --space TestSpace
```

```bash
uv run python rollover.py
```

```bash
uv run python rollover.py --force
```

Airtable Personal Access Token (scope `data.records:read` บน base นี้) อยู่ใน
`..\.env\Airtable.env` เป็น `AIRTABLE_TOKEN=pat...` หรือ token เปล่า ๆ บรรทัดเดียว

ตั้ง task:

```bash
schtasks /create /xml Rollover_task.xml /tn SimpleHeatmap_Rollover
```

ถ้า schtasks ฟ้อง `unable to switch the encoding` ให้แปลงไฟล์เป็น UTF-16 (และแก้ `encoding="UTF-16"`
ในบรรทัดแรก) ก่อน

---

## Permission ที่ต้องมี

| ชั้น | สิ่งที่ต้องมี |
|---|---|
| Google Sheets | `..\.cred\client_secret.json` + `authorized_user.json` (scope spreadsheets) บัญชีต้องเป็น Editor ของชีต |
| Google Chat | `..\.cred\token.json` (scope `chat.messages.create`) บัญชีต้องเป็นสมาชิกของ space |
| Chat space | id อยู่ใน `..\.env\Path.env` ตามคีย์ (`HeatmapSpace`, `TestSpace`) |
| Airtable | `..\.env\Airtable.env` |
| MT5 | terminal เปิดและล็อกอินค้างไว้ symbol อยู่ใน Market Watch |
| Windows | Task Scheduler รันภายใต้ user ที่ล็อกอินอยู่ (`InteractiveToken`) |

## Config (`config.json`)

| คีย์ | ใช้โดย | ความหมาย |
|---|---|---|
| `spreadsheet_id`, `sheet_name` | เก็บราคา | ชีตปลายทาง (`sheet_name` ว่าง = ชีตแรก) |
| `snapshot_times` | เก็บราคา | เวลา snapshot (เวลาไทย) ตอนนี้ `["10:00", "21:00"]` |
| `lookback_minutes` | เก็บราคา | ย้อนหา tick ได้กี่นาที (3) เกินนั้นเว้นว่าง |
| `max_backfill_days` | เก็บราคา | ย้อนเก็บวันที่ขาดได้ไกลสุดกี่วัน |
| `unquoted_symbols` | เก็บราคา | ขาที่มีช่องแล้วแต่ยังไม่ต้องดึงราคา |
| `timezone`, `log_file` | เก็บราคา | |
| `roll_hurdle_pct` | rollover | recommend yield (%) |
| `rollover_space_key` | rollover | คีย์ใน Path.env ของ space ที่ส่ง |

`MT5_PATH` อ่านจาก `..\.env\Path.env` Log อยู่ใน `logs/` (`simple_heatmap.log`, `rollover.log`)
