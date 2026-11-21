R="#c62828"; O="#e65100"; B="#1565c0"; K="#000"; G="#555"
out=[]
def line(pts,c=K,w=3,dash=None):
    for (a,b),(x2,y2) in zip(pts,pts[1:]):
        h=w/2
        if b==y2: out.append(f'<rect x="{min(a,x2)-h}" y="{b-h}" width="{abs(x2-a)+w}" height="{w}" style="fill:{c}"/>')
        elif a==x2: out.append(f'<rect x="{a-h}" y="{min(b,y2)-h}" width="{w}" height="{abs(y2-b)+w}" style="fill:{c}"/>')
        else: out.append(f'<line x1="{a}" y1="{b}" x2="{x2}" y2="{y2}" style="stroke:{c};stroke-width:{w}"/>')
def box(x1,y1,x2,y2,c=K,fill="#fafafa"):
    out.append(f'<rect x="{x1}" y="{y1}" width="{x2-x1}" height="{y2-y1}" style="fill:{fill}"/>')
    line([(x1,y1),(x2,y1),(x2,y2),(x1,y2),(x1,y1)],c,2.5)
def text(x,y,s,size=17,c=K,anchor="start",bold=False):
    s=s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    b=' font-weight="bold"' if bold else ""
    out.append(f'<text x="{x}" y="{y}" font-family="DejaVu Sans" font-size="{size}" fill="{c}" text-anchor="{anchor}"{b}>{s}</text>')
def gnd(x,y):
    line([(x,y),(x,y+12)]); line([(x-14,y+12),(x+14,y+12)]); line([(x-9,y+18),(x+9,y+18)]); line([(x-4,y+24),(x+4,y+24)])
def flag(x,y,label,c):  # wire going up from (x,y) to a net label
    line([(x,y),(x,y-22)],c); text(x,y-28,label,16,c,"middle",True)
def dot(x,y,c): out.append(f'<circle cx="{x}" cy="{y}" r="5" style="fill:{c}"/>')

W,H=1620,1000
text(30,42,"Cooking-coach wearable: wiring (NiMH + 3.3V LDO version)",26,bold=True)

# --- power: battery, switch, C1 ---
box(40,110,190,230); text(115,150,"NiMH pack",17,anchor="middle",bold=True); text(115,175,"4.8V 2200mAh",15,anchor="middle"); text(115,198,"(Hitec plug)",15,anchor="middle")
text(178,132,"+",20,R,bold=True); text(178,224,"−",22,bold=True)
line([(190,140),(225,140)],R); dot(225,140,R); line([(225,140),(265,120)],K,3); dot(270,140,R)
text(247,105,"SW1",15,anchor="middle"); text(247,170,"slide switch",13,G,"middle")
line([(270,140),(400,140)],R)
text(408,146,"→ VBAT",18,R,bold=True)
line([(115,230),(115,250)]); gnd(115,250)

# --- LDOs ---
for (y,name,net) in [(330,"U1",'3V3_A'),(600,"U2",'3V3_B')]:
    box(70,y,220,y+90,O,"#fff8f0")
    text(145,y+28,f"{name} MCP1826S-3.3",14,anchor="middle",bold=True); text(145,y+50,"3.3V 1A LDO, TO-220",13,anchor="middle"); text(145,y+70,"10µF in + out",13,G,"middle")
    line([(70,y+45),(40,y+45),(40,y+5)],R); text(40,y-2,"VBAT",15,R,"middle",True)
    text(76,y+86,"IN",12,G); text(194,y+86,"OUT",12,G)
    line([(145,y+90),(145,y+105)]); gnd(145,y+105)

# --- Freenove CAM ---
box(330,290,580,470); text(455,320,"Freenove ESP32",17,anchor="middle",bold=True); text(455,342,"WROVER CAM",17,anchor="middle",bold=True)
text(455,392,"camera + WiFi only,",14,G,"middle"); text(455,412,"no other wires",14,G,"middle")
line([(220,375),(330,375)],O); text(275,368,"3.3V",14,O,"middle"); text(338,380,"3V3",14,bold=True)
line([(455,470),(455,485)]); gnd(455,485); text(470,465,"GND",13)

# --- ESP32-S3 ---
box(330,560,620,910); text(475,592,"ESP32-S3 N16R8",18,anchor="middle",bold=True); text(475,612,"(don't use GPIO 35–37)",13,G,"middle")
line([(220,645),(330,645)],O); text(275,638,"3.3V",14,O,"middle"); text(338,650,"3V3",14,bold=True)
for y,lab in [(640,"GPIO4"),(680,"GPIO5"),(740,"GPIO6"),(790,"GPIO1"),(840,"GPIO8 SDA"),(880,"GPIO9 SCL")]:
    text(612,y+5,lab,14,anchor="end",bold=True)
line([(475,910),(475,925)]); gnd(475,925); text(490,905,"GND",13)

# --- servos ---
for (y,name,gp,route_x) in [(220,"Pan servo (SG90)",640,880),(380,"Tilt servo (SG90)",680,920)]:
    box(1050,y,1260,y+100); text(1155,y+40,name,16,anchor="middle",bold=True)
    text(1155,y+62,"red=V+  brown=GND  orange=sig",12,G,"middle")
    flag(1100,y,"VBAT",R)
    line([(620,gp),(route_x,gp),(route_x,y+80),(1050,y+80)],B)
    line([(1260,y+80),(1290,y+80),(1290,y+88)]); gnd(1290,y+88)
text(760,633,"pan signal",13,B,"middle"); text(760,673,"tilt signal",13,B,"middle")

# --- C1: physically AT the servo connectors ---
line([(1190,150),(1190,160)],R); text(1190,144,"VBAT",15,R,"middle",True)
line([(1174,160),(1206,160)],K,4); line([(1174,170),(1206,170)],K,4); text(1166,158,"+",15,R,anchor="end",bold=True)
line([(1190,170),(1190,182)]); gnd(1190,182)
text(1215,165,"C1 1000µF ≥10V",13,bold=True); text(1215,182,"right at servo plugs",12,G)

# --- laser + Q1 ---
box(1050,540,1260,630); text(1155,572,"KY-008 laser",16,anchor="middle",bold=True); text(1155,594,"S = +,  − = to Q1,",12,G,"middle"); text(1155,612,"middle pin: unused",12,G,"middle")
flag(1100,540,"VBAT",R)
line([(620,740),(680,740)],B); box(680,728,760,752,K,"#fff"); text(720,722,"R1 1kΩ",13,anchor="middle"); line([(760,740),(930,740)],B)
out.append('<circle cx="958" cy="740" r="31" style="fill:#000"/>'); out.append('<circle cx="958" cy="740" r="28.5" style="fill:#fff"/>')
line([(944,722),(944,758)],K,4); line([(930,740),(944,740)],K)
line([(944,732),(972,712),(972,610),(1050,610)]); line([(944,748),(972,768),(972,790)]); gnd(972,790)
out.append('<polygon points="972,768 958,766 965,754" style="fill:#000"/>')
text(1000,700,"C",13,G); text(1000,786,"E",13,G); text(920,733,"B",13,G)
text(958,830,"Q1 2N2222",14,anchor="middle",bold=True)

# --- OLED ---
box(1050,820,1260,930); text(1155,858,"OLED 0.96\" SSD1306",15,anchor="middle",bold=True); text(1155,880,"(optional, I2C)",13,G,"middle")
line([(620,840),(680,840),(680,850),(1050,850)],B); line([(620,880),(1050,880),(1050,880)],B)
text(1045,845,"SDA",12,anchor="end"); text(1045,875,"SCL",12,anchor="end")
line([(1150,820),(1150,800)],O); text(1150,794,"3V3 (from U2)",13,O,"middle",True)
line([(1260,900),(1290,900),(1290,908)]); gnd(1290,908)

# --- battery monitor (optional) ---
text(640,775,"optional battery meter:",12,G); text(640,808,"VBAT–100k–GPIO1–100k–GND",12,G)

# --- notes ---
nx=1330
text(nx,250,"NOTES",18,bold=True)
notes=["1. Every ground symbol is ONE","   shared rail. Connect them all.",
"2. VBAT (up to 5.6V) must never","   touch an ESP pin.",
"3. Unplug battery before USB","   flashing (3V3 back-feed).",
"4. U1/U2 get warm: TO-220, keep","   space around them.",
"5. Check LDO pinout in datasheet","   (MCP1826S: 1=IN 2=GND 3=OUT).",
"6. Check 2N2222 pinout for your","   package (TO-92: E-B-C).",
"7. Measure + / − on the Hitec","   plug with a meter first.",
"8. ~3h per pack. Swap to the","   2nd pack when servos slow."]
for i,s in enumerate(notes): text(nx,280+i*22,s,14)
text(nx,650,"Wire colours",16,bold=True)
for i,(c,l) in enumerate([(R,"VBAT 4.4–5.6V"),(O,"3.3V (regulated)"),(B,"signals 3.3V"),(K,"ground")]):
    line([(nx,672+i*24),(nx+40,672+i*24)],c,4); text(nx+50,678+i*24,l,14)

svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}"><rect width="{W}" height="{H}" fill="#fff"/>'+"".join(out)+'</svg>'
open("schematic.svg","w").write(svg)
