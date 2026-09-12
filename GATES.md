# Gates: Windows desktop app

Scope: تطبيق Windows عربي بـ Tkinter يشغّل الوكيل المحلي دون تجميد الواجهة ويحافظ على حدود الموافقة والأمان.

- [x] G1: تمر اختبارات الوكيل وواجهة سطح المكتب وتبقى تغطية كود التطبيق 80% على الأقل
  CHECK: `python -m coverage erase; python -m coverage run --branch --source=. --omit='test_*.py,build/*,dist/*' -m unittest; python -m coverage report --fail-under=80 -m`
  EVIDENCE: exit=0; Python 3.14; 129 tests OK; app-only branch coverage=81%.
  COMPATIBILITY: `py -3.12 -m unittest` → 129 tests OK.

- [x] G2: يمر Ruff وترجمة كل ملفات Python
  CHECK: `python -m ruff check .; py -3.12 -m compileall -q -f .`
  EVIDENCE: exit=0; `All checks passed!` وcompileall بلا أخطاء.

- [x] G3: يمكن إنشاء نافذة TkinterDnD وتبديل السمة وإغلاقها دون اتصال بالنموذج
  CHECK: `py -3.12 -c "from tkinterdnd2 import TkinterDnD; from desktop_app import LocalAgentApp; r=TkinterDnD.Tk(); r.withdraw(); a=LocalAgentApp(r); a.toggle_theme(); a.toggle_theme(); r.update_idletasks(); a.close(); print('G3_GUI_SMOKE_PASSED')"`
  EXPECT: `G3_GUI_SMOKE_PASSED`.

- [x] G4: تثبت معاينة مرئية للملف التنفيذي أن التخطيط العربي والسمتين وتبويب التغييرات وزر Undo قابلة للاستخدام
  EVIDENCE: فُحصت `dist\LocalAgent.exe` الفعلية عبر Computer Use عند 1884x1258؛ نجح تبديل الوضع الداكن وفتح تبويب التغييرات وإغلاق النافذة.

- [x] G5: لا تجد مراجعتا الكود والأمن المستقلتان عيوبًا عالية أو حرجة
  EVIDENCE: code review PASS; security review G5 PASS; لا توجد findings متبقية ضمن النطاق.

## حدود الدليل

- LM Studio لم يكن يحمّل نموذجًا أثناء التحقق؛ البث مثبت باختبارات HTTP/SSE محلية وليس بجلسة Qwen حية.
- Docker daemon غير عامل وPodman غير مثت؛ توليد argv المعزول وقيوده مثبتة بالاختبار، لكن لم تُشغّل حاوية فعلية.
