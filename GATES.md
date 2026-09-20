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

## إضافة DeepSeek Harness

- [x] G11: يطبّق ملف DeepSeek تعليمات الأدوات وإعداد التوليد المخصص، ويرفض أي معرّف Harness غير معروف
  CHECK: python -m unittest -v test_agent.ModelHarnessTests
  EXPECT: OK
  EVIDENCE: exit=0; نجح اختباران للتعليمات ودرجة الحرارة والتحقق من المعرّف.

- [x] G12: يُحفظ اختيار DeepSeek من الواجهة ويمر عبر DesktopConfig إلى العميل والوكيل
  CHECK: python -m unittest -v test_desktop_app.DesktopConfigTests test_desktop_app.DesktopUiTests
  EXPECT: OK
  EVIDENCE: exit=0; نجحت اختبارات الإعداد والواجهة، بما فيها حفظ DeepSeek واستعادته.

- [x] G13: تمر مجموعة الاختبارات وفحوص الجودة كاملة بعد الإضافة
  CHECK: python -m unittest && python -m ruff check . && python -m compileall -q -f . && echo DEEPSEEK_HARNESS_VERIFIED
  EXPECT: DEEPSEEK_HARNESS_VERIFIED
  EVIDENCE: exit=0; 232 اختبارًا OK؛ Ruff نجح؛ compileall اكتمل؛ تغطية التطبيق 80%.

- [x] G14: تُعاد حزم نسخة Windows المستقلة لتتضمن الخيار الجديد
  EVIDENCE: بُني `dist\LocalAgent.exe` بـ Python 3.12.10 وPyInstaller 6.21.0؛ اجتاز فحص تشغيل مخفي لمدة 5 ثوانٍ؛ الحجم 14,181,408 بايت؛ SHA256=`2FEC5BE41A3DF64455E39A2593ABFA70131FEBCCD878F25E6E162A2093983AAE`.

- [x] G15: تُضمّن سياسة AGENTS_PROGRAMMING_ONLY وتُحمّل تلقائيًا فقط في وضعي البرمجة
  CHECK: python -m unittest -v test_agent.ProgrammingInstructionsTests
  EXPECT: OK
  EVIDENCE: exit=0; نجح اختباران للتحميل الانتقائي ورفض الملف المفقود والكبير وغير UTF-8؛ كما تطابقت النسخة المضمنة مع الملف المرفق سطرًا بسطر.

- [x] G16: تحتوي نسخة Windows على ملف السياسة وتبدأ دون خروج مبكر
  EVIDENCE: أظهر PyInstaller archive `assets\AGENTS_PROGRAMMING_ONLY.md` بحجم 15,235 بايت؛ اجتاز الملف التنفيذي فحص تشغيل مخفي لمدة 5 ثوانٍ؛ الحجم 14,187,511 بايت؛ SHA256=`37C0DEF0E52A88F9283BA86B4CC9ADBD45DB14812EAE63A547A1C4833DC01C67`.
