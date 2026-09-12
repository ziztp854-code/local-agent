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
    # عناوين بأسلوب الطرفية: monospace يعطي طابع TUI حتى في النوافذ.
    if weight == "normal":
        return (MONO_FONT_FAMILY, size)
    return (MONO_FONT_FAMILY, size, weight)


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
# حواف شبه حادة بأسلوب الطرفية (terminal)؛ التفاصيل الأربعة تظل لطيفة في الفاتح.
RADIUS = {"sm": 3, "md": 5, "lg": 6}


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
    return (MONO_FONT_FAMILY, 9, "italic")


def tool_line_font():
    return (MONO_FONT_FAMILY, 8, "italic")


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

# هوية «طرفية OpenCode»: أسود محايد رمادي (ليس مخضرًّا) مع لمسة برتقالية واحدة،
# حواف شبه حادة وطبقة mono للتفاصيل — إحساس TUI داخل نافذة سطح مكتب.
# سلّم عمق محايد: fog (رئيسية) < secondary_bg < surface (بطاقات) < chip/elevated.
DARK_COLORS = {
    "ink": "#EDEDEE",       # نص رئيسي فاتح محايد
    "fog": "#0A0A0A",       # خلفية رئيسية شبه سوداء
    "surface": "#141414",   # خلفية البطاقات
    "muted": "#8A8A8A",     # نص ثانوي رمادي
    "teal": "#FF8F40",      # اللون الأساسي (برتقالي OpenCode)
    "copper": "#FFC878",    # تحذير/إبراز ثانوي
    "danger": "#FF6B6B",    # خطأ
    "line": "#262626",      # حدود خفيفة محايدة
    "soft_teal": "#1D1611",  # خلفية أساسية ناعمة (فقاعة المستخدم/النشط) بدفء برتقالي
    "soft_copper": "#211B10",
    "soft_danger": "#2A1418",
    "focus": "#FFA35C",     # الأساسي عند التحويم/حلقة التركيز
    "band_bg": "#0F0F0F",   # خلفية ثانوية (شريط الحالة)
    "band_fg": "#EDEDEE",
    "chip": "#1B1B1B",      # خلفية مرتفعة (الرقائق)
    "chip_fg": "#D4D4D4",
    "success": "#4AD98F",
    "warning": "#FFC878",
    "info": "#6FA8FF",
    "elevated": "#1B1B1B",
    "secondary_bg": "#0F0F0F",
}
