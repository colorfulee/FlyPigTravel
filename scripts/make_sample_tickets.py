"""生成测试 / 演示用的机票截图（虚构乘客，均标 SAMPLE）：英文行程单、首尔转机的中文行程单、只有起飞时刻的登机牌。
用法：python scripts/make_sample_tickets.py <输出目录>
app/static/sample_ticket.jpg 就是其中的英文行程单（缩到 900px）。
"""
from PIL import Image, ImageDraw, ImageFont
import sys
EN = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"; ENB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
import subprocess
ZH = subprocess.run(["fc-match", "-f", "%{file}", "WenQuanYi Zen Hei"], capture_output=True, text=True).stdout
f = lambda p, s: ImageFont.truetype(p, s)
out = sys.argv[1]

def watermark(d, w, h):
    d.text((w - 190, 18), "SAMPLE", font=f(ENB, 30), fill=(220, 60, 60))

# 1 英文电子客票行程单
im = Image.new("RGB", (1080, 1300), "white"); d = ImageDraw.Draw(im)
d.rectangle([0, 0, 1080, 110], fill=(14, 44, 92)); d.text((40, 32), "E-TICKET ITINERARY RECEIPT", font=f(ENB, 38), fill="white")
watermark(d, 1080, 110)
d.text((40, 140), "Passenger: SAMPLE/ALEX MR      Ticket No: 781-0000000000", font=f(EN, 26), fill="black")
d.text((40, 180), "Booking ref: XQ7ZPL      Issued: 12SEP26", font=f(EN, 26), fill=(80, 80, 80))
y = 250
for no, frm, to, dep, arr, cls in [
    ("MU586", "LOS ANGELES (LAX) T-B", "SHANGHAI PUDONG (PVG) T1", "25OCT  23:50", "27OCT  05:40", "Economy / Y"),
    ("3U3961", "CHENGDU TIANFU (TFU) T1", "SEOUL INCHEON (ICN) T1", "04NOV  13:30", "04NOV  18:40", "Economy / K"),
]:
    d.rounded_rectangle([30, y, 1050, y + 380], 18, outline=(190, 190, 190), width=3)
    d.text((60, y + 25), f"Flight {no}", font=f(ENB, 34), fill=(14, 44, 92)); d.text((700, y + 32), cls, font=f(EN, 24), fill=(90, 90, 90))
    d.text((60, y + 95), "Depart", font=f(EN, 22), fill=(110, 110, 110)); d.text((60, y + 125), frm, font=f(ENB, 28), fill="black")
    d.text((60, y + 165), dep, font=f(EN, 30), fill="black")
    d.text((60, y + 225), "Arrive", font=f(EN, 22), fill=(110, 110, 110)); d.text((60, y + 255), to, font=f(ENB, 28), fill="black")
    d.text((60, y + 295), arr, font=f(EN, 30), fill="black")
    y += 420
d.text((40, 1150), "All times are local. Baggage: 2PC. Check-in closes 60 min before departure.", font=f(EN, 22), fill=(110, 110, 110))
im.save(f"{out}/ticket_en.png")

# 2 中文行程单（首尔转机）
im = Image.new("RGB", (1080, 1500), (246, 247, 249)); d = ImageDraw.Draw(im)
d.text((40, 40), "机票行程单", font=f(ZH, 46), fill="black"); watermark(d, 1080, 100)
d.text((40, 115), "乘客：SAMPLE/JORDAN    订单号：2026101500000", font=f(ZH, 26), fill=(80, 80, 80))
y = 190
for no, airline, frm, to, dep, arr in [
    ("KE018", "大韩航空", "洛杉矶 洛杉矶国际机场 LAX TB", "首尔 仁川国际机场 ICN T2", "10月25日 23:30", "10月27日 05:10"),
    ("KE893", "大韩航空", "首尔 仁川国际机场 ICN T2", "北京 首都国际机场 PEK T2", "10月27日 08:40", "10月27日 09:45"),
    ("CA925", "中国国际航空", "北京 首都国际机场 PEK T3", "东京 成田国际机场 NRT T1", "11月5日 08:20", "11月5日 12:35"),
]:
    d.rounded_rectangle([30, y, 1050, y + 380], 20, fill="white")
    d.text((60, y + 25), f"{airline}  {no}", font=f(ZH, 34), fill=(0, 90, 170))
    d.text((60, y + 95), "出发  " + dep, font=f(ZH, 30), fill="black"); d.text((60, y + 140), frm, font=f(ZH, 28), fill=(60, 60, 60))
    d.text((60, y + 215), "到达  " + arr, font=f(ZH, 30), fill="black"); d.text((60, y + 260), to, font=f(ZH, 28), fill=(60, 60, 60))
    d.text((60, y + 320), "当地时间", font=f(ZH, 22), fill=(140, 140, 140))
    y += 410
im.save(f"{out}/ticket_zh.png")

# 3 登机牌（只有起飞时间）
im = Image.new("RGB", (1200, 520), "white"); d = ImageDraw.Draw(im)
d.rectangle([0, 0, 1200, 90], fill=(0, 102, 153)); d.text((30, 22), "BOARDING PASS", font=f(ENB, 40), fill="white"); watermark(d, 1200, 90)
for x, lab, val in [(30, "NAME", "SAMPLE/SAM"), (420, "FLIGHT", "KE893"), (700, "DATE", "26OCT"), (950, "SEAT", "32A")]:
    d.text((x, 120), lab, font=f(EN, 22), fill=(110, 110, 110)); d.text((x, 150), val, font=f(ENB, 34), fill="black")
for x, lab, val in [(30, "FROM", "SEOUL / ICN"), (420, "TO", "SHANGHAI / PVG"), (950, "GATE", "117")]:
    d.text((x, 240), lab, font=f(EN, 22), fill=(110, 110, 110)); d.text((x, 270), val, font=f(ENB, 34), fill="black")
for x, lab, val in [(30, "BOARDING", "12:40"), (420, "DEPARTURE", "13:20"), (700, "ZONE", "3")]:
    d.text((x, 360), lab, font=f(EN, 22), fill=(110, 110, 110)); d.text((x, 390), val, font=f(ENB, 34), fill="black")
im.save(f"{out}/boarding.png")
print("ok", ZH)
