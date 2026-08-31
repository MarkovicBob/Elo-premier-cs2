(() => {
  const STORAGE_KEY = "elo-live-language";
  const DEFAULT_LANGUAGE = "en";
  const SUPPORTED_LANGUAGES = ["en", "sr"];

  const translations = {
    en: {
      "nav.how": "HOW IT WORKS",
      "nav.preview": "PREVIEW",
      "nav.login": "LOGIN",
      "nav.logout": "Logout",
      "home.hero_sub":
        "Show your <strong>Premier ELO live</strong> on stream.<br />Log in once. It keeps working.",
      "home.login_steam": "Login with Steam",
      "home.active_streamers": "Active Streamers",
      "home.realtime_elo": "Real-time ELO",
      "home.platforms": "Platforms",
      "home.forever": "Forever",
      "home.how_title": "How it works",
      "home.step_login_title": "Log in with Steam",
      "home.step_login_desc":
        "One click, no password. We use official Steam OpenID, so your account details stay secure.",
      "home.step_url_title": "Get your URL",
      "home.step_url_desc":
        "We generate a unique Browser Source link connected to your Steam profile.",
      "home.step_stream_title": "Add it to your stream",
      "home.step_stream_desc":
        "Paste the URL into TikTok Live Studio, Streamlabs, or OBS as a Browser Source. Done.",
      "home.preview_title":
        "This is how it looks<br />on your <span style=\"color: var(--gold)\">livestreams</span>",
      "home.preview_copy":
        "A transparent overlay that fits any layout. Your ELO refreshes automatically every 2 minutes, with no button presses.",
      "home.cta_title": "Ready to go<br /><em>live?</em>",
      "home.cta_sub": "Free. Forever. No installation.",

      "dashboard.title": "Your Premier ELO",
      "dashboard.auto_refresh": "Refreshes automatically every 90s",
      "dashboard.style_tag": "// CHOOSE YOUR OVERLAY STYLE",
      "dashboard.style_title": "How your overlay looks on stream",
      "dashboard.style_copy":
        "The larger preview shows each overlay on a stylized scene, so you can immediately see which look fits best.",
      "dashboard.style_three": "Style 3 — ELO today",
      "dashboard.url_title": "// YOUR BROWSER SOURCE URL",
      "dashboard.url_note":
        "Add this URL to <strong>TikTok Live Studio</strong>, <strong>Streamlabs</strong>, or <strong>OBS</strong> as a Browser Source.",
      "dashboard.update_cookies": "↺ UPDATE COOKIES",
      "dashboard.api_key_missing": "Steam API Key is not configured",
      "dashboard.api_key_help":
        "The app can also work with connected Steam cookies, but an API key is a more reliable fallback if the cookies expire.<br><strong style=\"color:var(--text)\">1.</strong> Go to <a href=\"https://steamcommunity.com/dev/apikey\" target=\"_blank\" style=\"color:var(--gold)\">steamcommunity.com/dev/apikey</a> — it is free and takes about 30 seconds<br><strong style=\"color:var(--text)\">2.</strong> In Railway → Variables, add <code style=\"background:var(--bg3);padding:1px 6px;color:var(--gold)\">STEAM_API_KEY=your_key</code><br><strong style=\"color:var(--text)\">3.</strong> Redeploy — API updates will no longer depend on cookie lifetime",
      "dashboard.setup_title": "Enable ELO tracking",
      "dashboard.setup_desc":
        "Connect your Steam cookies for live ELO. The API key is optional, but useful as a fallback.",
      "dashboard.open_steam":
        "Open <a href=\"https://steamcommunity.com/login/home/\" target=\"_blank\" style=\"color:var(--gold)\">steamcommunity.com</a> and sign in to your Steam account",
      "dashboard.copy_cookies": "Copy sessionid and steamLoginSecure",
      "dashboard.session_placeholder": "e.g. 8d3af9c0bfd508070f3a0aea",
      "dashboard.tutorial_title": "// LIKE THIS:",
      "dashboard.save_cookies": "Save cookies",
      "dashboard.last_refresh": "Last refresh: {value}",
      "dashboard.since_last_check": "{diff} since the last check",
      "dashboard.copied": "COPIED!",
      "tutorial.step": "STEP {step} / 4",
      "tutorial.open_devtools": "Press <kbd>F12</kbd> to open Chrome DevTools",
      "tutorial.open_application":
        "Click the <strong>Application</strong> tab, then <strong>Cookies → steamcommunity</strong>",
      "tutorial.copy_session":
        "Find <strong>sessionid</strong> and copy its value",
      "tutorial.copy_login":
        "Find <strong>steamLoginSecure</strong> — click it to see the full value below",
      "message.fill_both": "Fill in both fields.",
      "message.success": "Success! ELO: {elo}",
      "message.fetching": "fetching...",
      "message.error": "Error: {error}",
      "message.unknown": "unknown error",
      "message.network": "Network error.",
      "error.missing_fields": "Fill in both fields.",
      "error.invalid_cookies":
        "The Steam cookies are invalid or cannot access the GCPD page. Copy fresh sessionid and steamLoginSecure values from your currently signed-in browser.",
    },
    sr: {
      "nav.how": "KAKO RADI",
      "nav.preview": "PRIKAZ",
      "nav.login": "PRIJAVA",
      "nav.logout": "Odjava",
      "home.hero_sub":
        "Prikaži svoj <strong>Premier ELO uživo</strong> na streamu.<br />Prijavi se jednom. Radi zauvek.",
      "home.login_steam": "Prijavi se putem Steama",
      "home.active_streamers": "Aktivni streameri",
      "home.realtime_elo": "ELO uživo",
      "home.platforms": "Platforme",
      "home.forever": "Zauvek",
      "home.how_title": "Kako funkcioniše",
      "home.step_login_title": "Prijavi se sa Steamom",
      "home.step_login_desc":
        "Jedan klik, bez lozinke. Koristimo zvaničan Steam OpenID — tvoji podaci su bezbedni.",
      "home.step_url_title": "Dobij svoj URL",
      "home.step_url_desc":
        "Generišemo jedinstveni Browser Source link koji je vezan za tvoj Steam profil.",
      "home.step_stream_title": "Dodaj u stream",
      "home.step_stream_desc":
        "Nalepi URL u TikTok Live Studio, Streamlabs ili OBS kao Browser Source. Gotovo.",
      "home.preview_title":
        "Izgleda ovako<br />na tvojim <span style=\"color: var(--gold)\">livestreamovima</span>",
      "home.preview_copy":
        "Transparentan overlay koji se uklapa uz svaki layout. ELO se osvežava automatski svakih 2 minuta bez pritiska dugmeta.",
      "home.cta_title": "Spreman za<br /><em>live?</em>",
      "home.cta_sub": "Besplatno. Zauvek. Bez instalacije.",

      "dashboard.title": "Tvoj Premier ELO",
      "dashboard.auto_refresh": "Osvežava se automatski svakih 90s",
      "dashboard.style_tag": "// IZABERI IZGLED OVERLAYA",
      "dashboard.style_title": "Kako overlay izgleda na streamu",
      "dashboard.style_copy":
        "Veći prikaz pokazuje overlay preko stilizovane scene, pa odmah možeš da vidiš koji izgled najbolje odgovara.",
      "dashboard.style_three": "Style 3 — ELO danas",
      "dashboard.url_title": "// TVOJ BROWSER SOURCE URL",
      "dashboard.url_note":
        "Dodaj ovaj URL u <strong>TikTok Live Studio</strong>, <strong>Streamlabs</strong> ili <strong>OBS</strong> kao Browser Source.",
      "dashboard.update_cookies": "↺ AŽURIRAJ COOKIES",
      "dashboard.api_key_missing": "Steam API Key nije postavljen",
      "dashboard.api_key_help":
        "Aplikacija može raditi i preko povezanih Steam cookiesa, ali API key je stabilniji fallback ako cookies isteknu.<br><strong style=\"color:var(--text)\">1.</strong> Idi na <a href=\"https://steamcommunity.com/dev/apikey\" target=\"_blank\" style=\"color:var(--gold)\">steamcommunity.com/dev/apikey</a> — besplatan key, oko 30 sekundi<br><strong style=\"color:var(--text)\">2.</strong> Na Railway → Variables dodaj <code style=\"background:var(--bg3);padding:1px 6px;color:var(--gold)\">STEAM_API_KEY=tvoj_key</code><br><strong style=\"color:var(--text)\">3.</strong> Uradi redeploy — API ažuriranje više neće zavisiti od trajanja cookiesa",
      "dashboard.setup_title": "Aktiviraj ELO tracking",
      "dashboard.setup_desc":
        "Poveži Steam cookies za ELO uživo. API key je opcioni, ali koristan fallback.",
      "dashboard.open_steam":
        "Otvori <a href=\"https://steamcommunity.com/login/home/\" target=\"_blank\" style=\"color:var(--gold)\">steamcommunity.com</a> i prijavi se na svoj Steam nalog",
      "dashboard.copy_cookies": "Kopiraj sessionid i steamLoginSecure",
      "dashboard.session_placeholder": "npr. 8d3af9c0bfd508070f3a0aea",
      "dashboard.tutorial_title": "// KAO OVO:",
      "dashboard.save_cookies": "Sačuvaj cookies",
      "dashboard.last_refresh": "Poslednje osvežavanje: {value}",
      "dashboard.since_last_check": "{diff} od prošle provere",
      "dashboard.copied": "KOPIRANO!",
      "tutorial.step": "KORAK {step} / 4",
      "tutorial.open_devtools": "Pritisni <kbd>F12</kbd> da otvoriš Chrome DevTools",
      "tutorial.open_application":
        "Klikni <strong>Application</strong> tab, zatim <strong>Cookies → steamcommunity</strong>",
      "tutorial.copy_session":
        "Pronađi <strong>sessionid</strong> i kopiraj vrednost",
      "tutorial.copy_login":
        "Pronađi <strong>steamLoginSecure</strong> — klikni da vidiš punu vrednost dole",
      "message.fill_both": "Popuni oba polja.",
      "message.success": "Uspešno! ELO: {elo}",
      "message.fetching": "preuzima se...",
      "message.error": "Greška: {error}",
      "message.unknown": "nepoznata greška",
      "message.network": "Mrežna greška.",
      "error.missing_fields": "Popuni oba polja.",
      "error.invalid_cookies":
        "Steam cookies nisu važeći ili nemaju pristup GCPD stranici. Kopiraj sveže sessionid i steamLoginSecure vrednosti iz browsera u kom si trenutno prijavljen.",
    },
  };

  function getStoredLanguage() {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      return SUPPORTED_LANGUAGES.includes(stored) ? stored : DEFAULT_LANGUAGE;
    } catch (_) {
      return DEFAULT_LANGUAGE;
    }
  }

  let currentLanguage = getStoredLanguage();

  function translate(key, variables = {}) {
    const dictionary = translations[currentLanguage] || translations[DEFAULT_LANGUAGE];
    const fallback = translations[DEFAULT_LANGUAGE][key] || key;
    const value = dictionary[key] || fallback;
    return value.replace(/\{(\w+)\}/g, (_, name) =>
      Object.prototype.hasOwnProperty.call(variables, name) ? variables[name] : `{${name}}`,
    );
  }

  function applyTranslations() {
    document.documentElement.lang = currentLanguage;

    document.querySelectorAll("[data-i18n]").forEach((element) => {
      element.textContent = translate(element.dataset.i18n);
    });
    document.querySelectorAll("[data-i18n-html]").forEach((element) => {
      element.innerHTML = translate(element.dataset.i18nHtml);
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((element) => {
      element.placeholder = translate(element.dataset.i18nPlaceholder);
    });
    document.querySelectorAll("[data-language]").forEach((button) => {
      const isActive = button.dataset.language === currentLanguage;
      button.classList.toggle("active", isActive);
      button.setAttribute("aria-pressed", String(isActive));
    });

    document.dispatchEvent(
      new CustomEvent("elo:language-changed", {
        detail: { language: currentLanguage },
      }),
    );
  }

  function setLanguage(language) {
    if (!SUPPORTED_LANGUAGES.includes(language)) return;
    currentLanguage = language;
    try {
      localStorage.setItem(STORAGE_KEY, language);
    } catch (_) {
      // The selected language still applies for the current page.
    }
    applyTranslations();
  }

  function initialize() {
    document.querySelectorAll("[data-language]").forEach((button) => {
      button.addEventListener("click", () => setLanguage(button.dataset.language));
    });
    applyTranslations();
  }

  window.EloI18n = {
    getLanguage: () => currentLanguage,
    setLanguage,
    t: translate,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, { once: true });
  } else {
    initialize();
  }
})();
