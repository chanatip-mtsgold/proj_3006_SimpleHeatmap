# proj_3006_SimpleHeatmap

ดึงราคา **bid/ask** ปัจจุบันจาก MetaTrader 5 ของทุก symbol ที่ระบุไว้ในแถวที่ 1 ของ
Google Sheet แล้ว **append** ต่อท้ายเป็นแถวใหม่ 1 แถวต่อการรัน 1 ครั้ง
(ตั้งเวลาไว้ทุกวันทำการ 10:00 น. เวลาไทย) เพื่อสะสมข้อมูลไว้ทำ heatmap

Sheet: `1MmAn7aVFIdReaMZgxoN-thw-LMtBFna4B5qCKI8kNpU`

## Sheet layout (worksheet `Data`)

|       | A          | B        | C        | D       | E        | F       |
|-------|------------|----------|----------|---------|----------|---------|
| แถว 1 | Date       | Time     | XAUUSD.s |         | CMX.GCV6 |         |
| แถว 2 |            |          | BID      | ASK     | BID      | ASK     |
| แถว 3 | 2026-09-04 | 10:00:01 | 4474.12  | 4474.60 | 4485.50  | 4485.90 |

- **แถว 1 คือ config** — คอลัมน์แรก ๆ ที่ชื่อ `Date` / `Time` / `Timestamp` คือบล็อกเวลา
  ส่วนที่เหลือคือชื่อ symbol โดยแต่ละตัวกิน 2 คอลัมน์ (BID, ASK)
  เพิ่ม/ลด/สลับ symbol ทำที่ sheet อย่างเดียว ไม่ต้องแก้โค้ด
- แถว 2 (BID/ASK) สคริปต์เขียนให้เองอัตโนมัติถ้ายังไม่มี
- ข้อมูลเริ่ม append ที่แถว 3 เสมอ
- **Timestamp คือเวลา snapshot ที่ตั้งไว้ (10:00:00) ไม่ใช่เวลาที่รันสคริปต์** — สคริปต์ดึง
  tick ตัวสุดท้าย ณ หรือก่อน 10:00 พอดี ผ่าน `copy_ticks_range` ดังนั้นรันกี่โมงก็ได้ผลเท่าเดิม
  (idempotent) และย้อนหลังได้
- Timestamp เป็น **เวลาไทย (GMT+7)**; MT5 เดินด้วย GMT+0 — เวลาของ tick จริง (UTC)
  ถูกบันทึกไว้ในไฟล์ log เพื่อใช้ตรวจว่าราคาค้าง (stale) หรือไม่
- symbol ไหนดึงราคาไม่ได้จะเว้นเซลล์ว่างไว้ (ไม่ใส่ 0)

## Permission ที่ต้องมี

| ชั้น | สิ่งที่ต้องมี |
|---|---|
| Google OAuth scope | `https://www.googleapis.com/auth/spreadsheets` |
| Credential | `..\.cred\client_secret.json` + `..\.cred\authorized_user.json` (ใช้ร่วมกับ proj_3003/3004) |
| สิทธิ์บน Sheet | บัญชี Google ของ `authorized_user.json` ต้องเป็น **Editor** ของสเปรดชีตนี้ |
| MT5 | terminal ต้องเปิดและ **ล็อกอินค้างไว้** (ต่อแบบ `mt5.initialize(path=...)` ไม่มี login/password) และ symbol ต้องอยู่ใน Market Watch |
| Windows | Task Scheduler รันภายใต้ user ที่ล็อกอินอยู่ (`InteractiveToken`) |
| ครั้งแรก | ถ้า token หมดอายุ/refresh ไม่ได้ ต้องรันแบบมี console 1 ครั้งเพื่อทำ OAuth ผ่านเบราว์เซอร์ |

**ไม่ต้องใช้:** service account, API key, Drive scope, MT5 investor/master password

## การใช้งาน

```bash
uv sync
```

ทดสอบโดยไม่เขียนลง sheet:

```bash
uv run python simple_heatmap.py --dry-run
```

รันจริง (snapshot ของ 10:00 วันนี้):

```bash
uv run python simple_heatmap.py
```

ย้อนหลัง (snapshot ของ 10:00 ในวันที่ระบุ):

```bash
uv run python simple_heatmap.py --date 2026-09-03
```

ตั้ง scheduled task (ทุกวันจันทร์–ศุกร์ 10:00):

```bash
schtasks /create /xml SimpleHeatmap_task.xml /tn SimpleHeatmap
```

ยิงทดสอบทันที:

```bash
schtasks /run /tn SimpleHeatmap
```

## Config

`config.json` — `spreadsheet_id`, `sheet_name` (เว้นว่าง = ใช้ชีตแรก), `log_file`,
`snapshot_time` (เวลาที่ต้องการ snapshot, เวลาไทย), `lookback_minutes`
(ย้อนหาราคาถอยหลังกี่นาทีถ้าไม่มี tick พอดีเวลานั้น), `timezone`

`MT5_PATH` อ่านจาก `..\.env\Path.env` ที่ใช้ร่วมกันทั้ง repo

Log: `logs/simple_heatmap.log` (rotate 1 MB × 5)
