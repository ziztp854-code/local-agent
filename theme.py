import tkinter.font as tkfont


UI_FONT_FAMILY = "Segoe UI"
DISPLAY_FONT_FAMILY = "Segoe UI Variable Display"
MONO_FONT_FAMILY = "Cascadia Mono"

_UI_CANDIDATES = ("Dubai", "Segoe UI Variable Text", "Segoe UI", "Tahoma")
_DISPLAY_CANDIDATES = ("Dubai", "Segoe UI Variable Display", "Segoe UI", "Tahoma")
_MONO_CANDIDATES = ("Cascadia Code", "Cascadia Mono", "Consolas", "Courier New")


def detect_fonts(root):
    global UI_FONT_FAMILY, DISPLAY_FONT_FAMILY, MONO_FONT_FAMILY
    try:
        families = set(tkfont.families(root))
    except tkfont.TclError:
        return (UI_FONT_FAMILY, DISPLAY_FONT_FAMILY, MONO_FONT_FAMILY)
    selections = {}
    for key, candidates in (
        ("UI", _UI_CANDIDATES),
        ("DISPLAY", _DISPLAY_CANDIDATES),
        ("MONO", _MONO_CANDIDATES),
    ):
        for candidate in candidates:
            if candidate in families:
                selections[key] = candidate
                break
    UI_FONT_FAMILY = selections.get("UI", UI_FONT_FAMILY)
    DISPLAY_FONT_FAMILY = selections.get("DISPLAY", DISPLAY_FONT_FAMILY)
    MONO_FONT_FAMILY = selections.get("MONO", MONO_FONT_FAMILY)
    return (UI_FONT_FAMILY, DISPLAY_FONT_FAMILY, MONO_FONT_FAMILY)


def ui_font(size=10, weight="normal"):
    if weight == "normal":
        return (UI_FONT_FAMILY, size)
    return (UI_FONT_FAMILY, size, weight)


def display_font(size=22, weight="bold"):
    if weight == "normal":
        return (DISPLAY_FONT_FAMILY, size)
    return (DISPLAY_FONT_FAMILY, size, weight)


def mono_font(size=10, weight="normal"):
    if weight == "normal":
        return (MONO_FONT_FAMILY, size)
    return (MONO_FONT_FAMILY, size, weight)


# مقياس مسافات موحّد (شبكة 4px) لاتساق الحشو والفراغات عبر الواجهة.
SPACE = {
    "xs": 4,
    "sm": 8,
    "md": 12,
    "lg": 16,
    "xl": 24,
    "2xl": 32,
}

# نصف قطر الزوايا الموحّد للبطاقات والأزرار.
RADIUS = {"sm": 6, "md": 10, "lg": 14}


def shade(hex_color, factor):
    """اطرح/أضف إضاءة للون سداسي عشري لتوليد حالة hover/الضغط.

    factor موجب يفتّح اللون ناحية الأبيض، وسالب يعتّمه ناحية الأسود،
    بمقدار نسبي بين -1 و1. يُعيد اللون كما هو عند مدخل غير صالح.
    """
    if not isinstance(hex_color, str):
        return hex_color
    value = hex_color.strip().lstrip("#")
    if len(value) != 6:
        return hex_color
    try:
        red, green, blue = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return hex_color
    factor = max(-1.0, min(1.0, factor))

    def mix(channel):
        target = 255 if factor >= 0 else 0
        return round(channel + (target - channel) * abs(factor))

    return f"#{mix(red):02X}{mix(green):02X}{mix(blue):02X}"


def relative_luminance(hex_color):
    """أعِد الإضاءة النسبية (0=أسود، 1=أبيض) للون سداسي عشري؛ 0.5 عند تعذّر القراءة."""
    if not isinstance(hex_color, str):
        return 0.5
    value = hex_color.strip().lstrip("#")
    if len(value) != 6:
        return 0.5
    try:
        red, green, blue = (int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return 0.5
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def space(*keys):
    """أعِد قيمة مسافة واحدة أو صفّ (padx/pady) من المقياس الموحّد.

    space("md") -> 12 ؛ space("lg", "sm") -> (16, 8)
    """
    values = tuple(SPACE[key] for key in keys)
    return values[0] if len(values) == 1 else values


def thought_font():
    return (UI_FONT_FAMILY, 9, "italic")


def tool_line_font():
    return (UI_FONT_FAMILY, 8, "italic")


def menu_font():
    return (UI_FONT_FAMILY, 9)


# Design Tokens — مصدر مركزي واحد للألوان. لا تكرّر قيمًا سداسية داخل الملفات؛
# استعمل مفاتيح هذه اللوحة عبر COLORS/DARK_COLORS.
COLORS = {
    "ink": "#152420",
    "fog": "#EEF3F1",
    "surface": "#FFFFFF",
    "muted": "#5B726B",
    "teal": "#00A277",
    "copper": "#B07A16",
    "danger": "#D33B52",
    "line": "#D3E0DB",
    "soft_teal": "#DCF3EC",
    "soft_copper": "#F6EBD9",
    "soft_danger": "#FBE3E7",
    "focus": "#00C08D",
    "band_bg": "#E6EEEB",
    "band_fg": "#152420",
    "chip": "#F4F8F6",
    "chip_fg": "#2A3B36",
    "success": "#137A52",
    "warning": "#B07A16",
    "info": "#2563EB",
    "elevated": "#FFFFFF",
    "secondary_bg": "#E6EEEB",
}

# هوية «أخضر زمردي على قماش داكن» احترافية بطابع تقني هادئ.
# سلّم عمق: fog (رئيسية) < secondary_bg < surface (بطاقات) < chip/elevated.
DARK_COLORS = {
    "ink": "#F3F7F5",       # نص رئيسي
    "fog": "#07110F",       # خلفية رئيسية
    "surface": "#10201C",   # خلفية البطاقات
    "muted": "#9AB0A9",     # نص ثانوي
    "teal": "#00D99A",      # اللون الأساسي
    "copper": "#F5B942",    # تحذير/إبراز ثانوي
    "danger": "#FF647C",    # خطأ
    "line": "#223A33",      # حدود خفيفة
    "soft_teal": "#0C2A22",  # خلفية أساسية ناعمة (فقاعة المستخدم/النشط)
    "soft_copper": "#2C2612",
    "soft_danger": "#2E1820",
    "focus": "#19E6AC",     # الأساسي عند التحويم/حلقة التركيز
    "band_bg": "#0B1714",   # خلفية ثانوية (شريط الحالة)
    "band_fg": "#F3F7F5",
    "chip": "#142823",      # خلفية مرتفعة (الرقائق)
    "chip_fg": "#D7E6E0",
    "success": "#25C990",
    "warning": "#F5B942",
    "info": "#55A7FF",
    "elevated": "#142823",
    "secondary_bg": "#0B1714",
}
