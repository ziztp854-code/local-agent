"""تشغيل تلقائي لخادم LM Studio مع تفريغ كامل للنموذج على كرت الشاشة (GPU).

الهدف: عند بدء التطبيق، إن لم يكن خادم LM Studio يعمل، شغّله محليًا وحمّل النموذج
على GPU حتى تكون الاستجابة سريعة دون خطوات يدوية. كل شيء محلي (127.0.0.1) ولا
يُطلق أي أمر شبكي خارجي؛ يقتصر التشغيل على أداة lms المثبتة على الجهاز.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from desktop_core import probe_model_server

# صيغة تشغيل هادئة على ويندوز (بدون فتح نافذة كونسول).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_lms():
    """اعثر على أداة lms التنفيذية، أو أعِد None إن لم تُثبّت."""
    found = shutil.which("lms")
    if found:
        return found
    candidate = Path.home() / ".lmstudio" / "bin" / ("lms.exe" if os.name == "nt" else "lms")
    return str(candidate) if candidate.exists() else None


def _run(lms, args, timeout):
    """نفّذ أمر lms بهدوء وأعِد (نجاح, مخرجات مختصرة)."""
    try:
        result = subprocess.run(
            [lms, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output.strip()[-500:]


def ensure_server(
    base_url,
    model,
    *,
    context_length=16384,
    ttl=1800,
    gpu="max",
    ready_timeout=90,
):
    """تأكد أن خادم LM Studio يعمل والنموذج محمّل على GPU.

    يعيد (ok, detail). إن كان الخادم يعمل أصلًا فلا يفعل شيئًا (لا يعيد التحميل).
    يقتصر على الخوادم المحلية فقط تماشيًا مع سياسة الوكيل.
    """
    parsed = urlparse(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return False, "يُسمح بالخادم المحلي فقط"

    ok, _detail = probe_model_server(base_url, timeout=2)
    if ok:
        return True, "الخادم يعمل مسبقًا"

    lms = find_lms()
    if not lms:
        return False, "أداة lms غير موجودة؛ افتح تطبيق LM Studio يدويًا"

    port = parsed.port or 1234
    started, out = _run(
        lms,
        ["server", "start", "--port", str(port)],
        timeout=30,
    )
    if not started:
        return False, f"تعذر تشغيل خادم LM Studio: {out}"

    # حمّل النموذج مع تفريغ كامل على كرت الشاشة (قد يستغرق ثوانٍ لأول تحميل).
    loaded, out = _run(
        lms,
        [
            "load", model,
            "--gpu", gpu,
            "--context-length", str(context_length),
            "--ttl", str(ttl),
            "-y",
        ],
        timeout=ready_timeout,
    )
    if not loaded:
        return False, f"تعذر تحميل النموذج على GPU: {out}"

    # انتظر حتى يستجيب الخادم فعليًا.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        ok, _detail = probe_model_server(base_url, timeout=2)
        if ok:
            return True, "تم تشغيل الخادم وتحميل النموذج على GPU"
        time.sleep(1)
    return True, "تم تحميل النموذج؛ الخادم يستكمل الإقلاع"
