> به‌روزرسانی ۱۷ اکتبر: شبیه‌سازی اکنون با منبع عمومی قرارداد OKX اجرا و بررسی شده است. محدودیت اجرای واقعی LBank همچنان برقرار است. راهنمای جاری: [DEPLOY_FA.md](DEPLOY_FA.md). توضیحات زیر درباره ناتمام بودن شبیه‌سازی مربوط به نسخه قبلی و منبع کندل خود LBank است.

# وضعیت و راه‌اندازی پروژه برای LBank

هدف پروژه مطابق درخواست کاربر **LBank Futures** است. پیش‌فرض تنظیمات دوباره `EXCHANGE_ID=lbank` است و هیچ صرافی جایگزینی به‌صورت خودکار انتخاب نمی‌شود.

## چرا نسخه فعلی کامل اجرا نمی‌شود؟

در نسخه نصب‌شده CCXT، بازار و قیمت قرارداد LBank قابل دریافت‌اند، اما تابع کندل به مسیر Spot درخواست می‌دهد و تابع سفارش هم پیاده‌سازی سفارش نقدی است. بنابراین انتخاب نام `lbank` به‌تنهایی، اجرای فیوچرز را فراهم نمی‌کند. این محدودیت اتصال فعلی است، نه اثبات نبود API فیوچرز در خود LBank.

راهنمای رسمی ساخت API، مجوز **Futures** را نام می‌برد. مستندات عمومی قراردادها نیز درباره احراز هویت و مجوز قرارداد توضیح می‌دهد؛ با این حال صفحه قابل دسترس فقط جزئیات چند مسیر عمومی بازار را دارد. مشخصات کامل درخواست/پاسخ کندل، موجودی فیوچرز، پوزیشن، سفارش، لغو، اهرم، مارجین ایزوله و حد ضرر در آن منتشر نشده است.

- [راهنمای رسمی ساخت API و گزینه Futures](https://support.lbank.com/hc/en-gb/articles/21535051026457-How-to-Create-and-Manage-Your-API-Web)
- [مستندات رسمی قرارداد LBank](https://www.lbank.com/docs/contract.html)
- [وضعیت اتصال LBank در CCXT](https://docs.ccxt.com/docs/comparisons/ccxt-vs-lbank-api)

**ثبت سفارش واقعی فیوچرز LBank هنوز پیاده‌سازی نشده است.** صرفاً برداشتن قفل اجرای LIVE این نقص را رفع نمی‌کند. استفاده از کندل Spot به‌جای کندل قرارداد هم در این پروژه به‌صورت خودکار انجام نمی‌شود.

در آزمایش فقط‌خواندنی ۱۵ اکتبر ۲۰۲۶، دریافت مشخصات و قیمت واقعی `BTC/USDT:USDT` از LBank موفق بود؛ دریافت کندل با `BadSymbol` شکست خورد. پس از بازگرداندن پیش‌فرض LBank، هر ۲۵ تست آفلاین نیز موفق بودند. این تست‌ها به معنی پیاده‌سازی سفارش واقعی LBank نیستند.

## کاری که اکنون بدون هیچ کلیدی می‌توانید انجام دهید

محیط مجازی همین پروژه نصب شده است. در PowerShell:

```powershell
Set-Location D:\MyProjects\Traider
.\.venv\Scripts\python.exe diagnose_lbank.py
```

این ابزار فقط از LBank داده عمومی می‌خواند؛ به توکن تلگرام، Gemini، API Key یا Secret نیاز ندارد و هیچ سفارشی ارسال نمی‌کند. کد خروج `2` یعنی داده عمومی بررسی شده اما اتصال معاملاتی آماده نیست؛ کد `1` یعنی بررسی عمومی هم خطا داشته است.

برای نصب از ابتدا در سیستم دیگر:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe diagnose_lbank.py
```

اگر `.env` موجود است آن را جایگزین نکنید. تغییر تنظیمات به `DRY_RUN=false` محدودیت پیاده‌سازی را حل نمی‌کند. اجرای کامل شبیه‌سازی خودکار نیز به دریافت کندل معتبر قرارداد نیاز دارد و هنوز برای LBank تأیید نشده است.

## برای تکمیل دقیقاً چه چیزی لازم است؟

مستندات معتبر API فیوچرز باید مسیرها، پارامترها، واحد حجم، کد وضعیت سفارش، احراز هویت و رفتار بستن پوزیشن را مشخص کند. داشتن API Key نقدی به‌تنهایی این اطلاعات را فراهم نمی‌کند. در این مرحله نیازی به ارسال کلید یا اطلاعات محرمانه نیست.

متن آماده برای دریافت اطلاعات از پشتیبانی رسمی LBank، در صورت نیاز:

> Hello. I am implementing a Python bot for LBank USDT perpetual futures using official API credentials. Your public contract documentation lists market endpoints, but does not list the complete trading endpoints. Please provide the current official Futures REST/WebSocket API specification, including historical 1m/15m candles, futures balances and positions, order placement/query/cancellation, isolated margin and leverage, reduce-only closing, stop-loss orders, authentication examples, API permission requirements, and sandbox availability. Does futures API access require account approval or whitelisting?

این متن آماده شده و برای هیچ‌کس ارسال نشده است. بعد از دسترسی به مشخصات معتبر، آداپتر مستقیم باید پیاده‌سازی و با پاسخ‌های نمونه و حساب مجاز تأیید شود. تا آن زمان وضعیت درست پروژه «زیرساخت آماده، اتصال معاملاتی LBank ناتمام» است.

راهنمای `README_FA.md` جزئیات نصب زیرساخت، تلگرام، Gemini و فرمان‌ها را نگه می‌دارد؛ مثال‌های OKX آن متعلق به مسیر آزمایش‌شده قبلی‌اند و به معنی تکمیل خواسته LBank نیستند.
