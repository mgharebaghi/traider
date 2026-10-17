# بررسی اتصال واقعی LBank — ۱۷ اکتبر ۲۰۲۶

**هدف، معامله واقعی فیوچرز با حد سود و ضرر ثبت‌شده روی صرافی است. این هدف هنوز تکمیل نشده است.** ابزار جدید `lbank_access_check.py` دسترسی حساب را بررسی می‌کند و هیچ قابلیت ثبت سفارش یا فعال‌سازی LIVE ندارد.

## نتایج آزمون با کلید موجود در .env

مقادیر کلید و Secret در خروجی یا این سند درج نشده‌اند. فقط درخواست‌های خواندنی به میزبان‌های رسمی ارسال شدند:

| آزمون | نتیجه |
| --- | --- |
| استعلام امضاشده مجوز API | HTTP 200، کد صفر |
| مجوز خواندن | فعال |
| مجوز Futures | فعال |
| مجوز Spot / برداشت / انتقال | غیرفعال |
| محدودیت IP | فعال؛ IPهای مجاز در این آزمون خوانده نشدند |
| زمان عمومی میزبان قراردادها | HTTP 200، success=true |
| خواندن حساب قراردادها با GET امضاشده | HTTP 403، پاسخ HTML |
| خواندن همان حساب با JSON POST امضاشده | HTTP 403، پاسخ HTML |
| درخواست بدون کلید به همان مسیر حساب | HTTP 403، پاسخ HTML |

این نتایج اعتبار امضای درخواست مجوز و فعال بودن پرچم فیوچرز را تأیید می‌کنند. پاسخ HTML 403 کد احراز هویت خود API نیست؛ علت آن از این آزمایش مشخص نمی‌شود. مجوز فیوچرز به‌تنهایی موفقیت اتصال معاملاتی را ثابت نمی‌کند. هیچ سفارش، لغو سفارش، انتقال وجه یا تغییر اهرم درخواست نشد.

## یافته تازه در SDK

پکیج `lbank-connector-python` که از مخزن معرفی‌شده در مستندات رسمی منتشر شده، در توضیحات نسخه‌های قدیمی `1.0.12` و `1.0.13` نمونه ثبت قرارداد با مسیر `/cfd/openApi/v1/prv/placeOrder` دارد. در بررسی قبلی این نمونه پیدا نشده بود؛ وجود آن را اکنون ثبت و نتیجه قبلی را دقیق‌تر می‌کنیم: مسیر نمونه ورود وجود دارد، ولی آداپتور کامل و تأییدشده معامله فیوچرز هنوز در پروژه وجود ندارد.

این نمونه، مشخصات کامل معنای مقادیر `offsetFlag`، `orderPriceType`، واحد حجم، خروج فقط‌کاهنده، تشخیص پرشدن، پوزیشن، لغو و TP/SL را ارائه نمی‌کند. توضیح امضای `/prv/account` نیز در مستندات قرارداد آمده، اما جدول کامل پارامترها و پاسخ آن موجود نیست. SDKهای رسمی Java، Go، Node و نسخه فعلی Python نیز برای مسیرهای لازم TP/SL مستندات کامل پیدا‌شده‌ای نداشتند. کد SDK دانلودشده نصب یا اجرا نشد؛ صرفاً منبع آن بررسی شد.

لینکی که از حساب My API ارائه شد، `https://www.lbank.com/docs/index.html#introduction` است. بخش سفارش آن Spot است؛ لینک Contract Trade API به همان سند عمومی قرارداد با مسیرهای بازار و توضیح امضا می‌رود.

منابع اولیه:

- [مرجع رسمی API و لینک SDKها](https://www.lbank.com/docs/index.html#introduction)
- [مرجع رسمی قراردادها](https://www.lbank.com/docs/contract.html)
- [نمونه قدیمی ثبت قرارداد در نسخه 1.0.12](https://pypi.org/project/lbank-connector-python/1.0.12/)
- [نمونه نسخه 1.0.13](https://pypi.org/project/lbank-connector-python/1.0.13/)
- [مخزن معرفی‌شده در مرجع رسمی](https://github.com/LBank-exchange/lbank-connector-python)

## اجرای آزمون روی همان سرور ربات

پس از دریافت این نسخه و ساخت مجدد تصویر، در پوشه پروژه:

```bash
docker compose build
# توقف یا شروع ربات برای این آزمون لازم نیست؛ ابزار وارد چرخه معاملات نمی‌شود.
docker compose run --rm --no-deps scalper-agent python lbank_access_check.py --account-post
```

در ترمینال کانتینر Coolify:

```bash
python lbank_access_check.py --account-post
```

برای مقایسه GET، گزینه `--account-post` را حذف کنید. این ابزار فقط همان سه مسیر ثابت و خواندنی را فراخوانی می‌کند؛ آدرس دلخواه، سفارش یا انتقال نمی‌پذیرد. توکن تلگرام یا کلید جمنای لازم نیست. کلید صرافی از `.env` یا متغیرهای محیطی سرویس خوانده می‌شود؛ در خروجی چاپ نمی‌شود و دنبال‌کردن redirect غیرفعال است. کد خروج ۲ عمدی است: حتی موفقیت آزمون خواندن، تأیید آمادگی LIVE محسوب نمی‌شود.

اگر روی سرور هم پاسخ حساب 403 بود، خروجی مجوزها و وضعیت HTTP را به پشتیبانی LBank بدهید؛ Secret یا کلید کامل را ارسال نکنید. محدودیت IP را با IP خروجی سرور تطبیق دهید؛ برای آزمایش آن را عمومی نکنید. اگر روی سرور پاسخ حساب موفق بود، هنوز باید مشخصات کامل سفارش و محافظ‌های آن دریافت و تست شود.

## اطلاعات لازم از پشتیبانی

متن آماده زیر برای هیچ‌کس ارسال نشده است:

> My API restriction query succeeds and reports enableReading=true, enableFuturesTrading=true, enableSpotTrading=false, ipRestrict=true. The public futures getTime endpoint succeeds, but GET and JSON POST to /cfd/openApi/v1/prv/account return HTTP 403 HTML from my environment. Please confirm the current supported USDT perpetual API base URL and account-read endpoint, IP/access requirements, and provide the current full futures REST/WebSocket specification and sandbox access. I found the historical placeOrder example in lbank-connector-python 1.0.12. I need documented entry/exit enums, quantity units, position queries, order status/cancellation, isolated margin, leverage, native stop-loss and take-profit, reduce-only/OCO behavior, client-order-id reconciliation, authentication and rate limits. Does enabling Futures on my API key require a separate activation or whitelist for these endpoints?

تا وقتی خواندن حساب/پوزیشن و ثبت و بازیابی TP/SL قابل تأیید نباشند، اجرای واقعی LBank در کد بسته می‌ماند. تغییر `DRY_RUN=false` یا برداشتن قفل، جای پیاده‌سازی اتصال را نمی‌گیرد. شبیه‌ساز قبلی با داده قرارداد OKX قابل اجراست؛ آن را معامله LBank تلقی نکنید.
