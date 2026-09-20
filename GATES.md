# Gates: Arabic delegated coding desktop app

Scope: تطبيق Windows عربي محلي أولًا يدير دورة تنفيذ ثم مراجعة مستقلة ثم اختبارات، ويعرض Diff قبل اعتماد النتيجة مع الحفاظ على حدود الموافقة والأمان.

- [x] G1: تمر اختبارات الوكيل وواجهة سطح المكتب وتبقى تغطية كود التطبيق 80% على الأقل
  CHECK: `python -m coverage erase; python -m coverage run --branch --source=. --omit='test_*.py,build/*,dist/*' -m unittest; python -m coverage report --fail-under=80 -m`
  EXPECT: TOTAL
  EVIDENCE: exit=0; Python 3.14; 129 tests OK; app-only branch coverage=81%.
  COMPATIBILITY: `py -3.12 -m unittest` → 129 tests OK.

- [x] G2: يمر Ruff وترجمة كل ملفات Python
  CHECK: `python -m ruff check .; py -3.12 -m compileall -q -f .`
  EXPECT: All checks passed!
  EVIDENCE: exit=0; `All checks passed!` وcompileall بلا أخطاء.

- [x] G3: يمكن إنشاء نافذة TkinterDnD وتبديل السمة وإغلاقها دون اتصال بالنموذج
  CHECK: python -c "from tkinterdnd2 import TkinterDnD; from desktop_app import LocalAgentApp; r=TkinterDnD.Tk(); r.withdraw(); a=LocalAgentApp(r); a.toggle_theme(); a.toggle_theme(); r.update_idletasks(); a.close(); print('G3_GUI_SMOKE_PASSED')"
  EXPECT: G3_GUI_SMOKE_PASSED
  EVIDENCE: exit=0; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Projects\local-agent; path=f875569ea789/61 entries; output=G3_GUI_SMOKE_PASSED

- [x] G4: تثبت معاينة مرئية للملف التنفيذي أن التخطيط العربي والسمتين وتبويب التغييرات وزر Undo قابلة للاستخدام
  EVIDENCE: فُحصت `dist\LocalAgent.exe` الفعلية عبر Computer Use عند 1884x1258؛ نجح تبديل الوضع الداكن وفتح تبويب التغييرات وإغلاق النافذة.

- [x] G5: لا تجد مراجعتا الكود والأمن المستقلتان عيوبًا عالية أو حرجة
  EVIDENCE: code review PASS; security review G5 PASS; لا توجد findings متبقية ضمن النطاق.

## حدود الدليل

- LM Studio لم يكن يحمّل نموذجًا أثناء التحقق؛ البث مثبت باختبارات HTTP/SSE محلية وليس بجلسة Qwen حية.
- Docker daemon غير عامل وPodman غير مثت؛ توليد argv المعزول وقيوده مثبتة بالاختبار، لكن لم تُشغّل حاوية فعلية.

- [x] G6: تدير طبقة التفويض دورة المهمة من العامل إلى المراجع والفحوصات ولا تسمح بالاعتماد قبل نجاح البوابات
  CHECK: python -m unittest -v test_delegation.py
  EXPECT: OK
  EVIDENCE: exit=0; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Projects\local-agent; path=f875569ea789/61 entries; output=Ran 7 tests in 0.014s | OK

- [x] G7: تعرض واجهة سطح المكتب المهام المفوضة وحالاتها ونتيجة المراجعة والاختبارات مع إجراء اعتماد صريح
  CHECK: python -m unittest -v test_desktop_app.py
  EXPECT: OK
  EVIDENCE: exit=0; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Projects\local-agent; path=f875569ea789/61 entries; output=Ran 46 tests in 5.615s | OK

- [x] G8: تمر مجموعة الاختبارات كاملة وتبقى تغطية كود التطبيق 80% على الأقل
  CHECK: python -m coverage erase && python -m coverage run --branch --source=. --omit=test_*.py,build/*,dist/* -m unittest && python -m coverage report --fail-under=80 -m
  EXPECT: TOTAL
  EVIDENCE: exit=0; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Projects\local-agent; path=f875569ea789/61 entries; output=Ran 230 tests in 30.026s | OK

- [x] G9: يمر Ruff وترجمة ملفات Python بعد إضافة المعمارية الجديدة
  CHECK: python -m ruff check . && python -m compileall -q -f . && echo QUALITY_CHECK_PASSED
  EXPECT: QUALITY_CHECK_PASSED
  EVIDENCE: exit=0; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Projects\local-agent; path=f875569ea789/61 entries; output=All checks passed! | QUALITY_CHECK_PASSED

- [x] G10: تعرض المعاينة المرئية لوحة دورة المهمة بالعربية دون قص أو تجميد، مع وضوح بوابات التنفيذ والمراجعة والاختبارات
  EVIDENCE: فُحصت `dist\LocalAgent.exe` الفعلية عبر Computer Use؛ ظهرت لوحة المهام العربية بأربع مراحل واضحة (التنفيذ، المراجعة، الاختبارات، الاعتماد) دون قص أو تجميد، ثم أُغلقت النسخة التجريبية بنجاح.
