(function () {
    var IDLE_MS      = 5 * 60 * 1000;   // keep the same as IDLE_SECONDS in app.py
    var WARN_MS      = 4.5 * 60 * 1000;
    var HEARTBEAT_MS = 60 * 1000;
    var ONLINE_MS    = 20 * 1000;

    var lastInteraction = Date.now();
    var pending = true;
    var banner = null;
    var leaving = false;      // true when we are signing out on purpose

    function interacted() {
        lastInteraction = Date.now();
        pending = true;
        if (banner) banner.style.display = 'none';
    }
    ['mousedown', 'keydown', 'touchstart', 'scroll', 'click'].forEach(function (ev) {
        window.addEventListener(ev, interacted, { passive: true });
    });
    var lastMove = 0;
    window.addEventListener('mousemove', function () {
        var n = Date.now();
        if (n - lastMove > 5000) { lastMove = n; interacted(); }
    }, { passive: true });

    // Pressing Sign Out records the exact sign-out time
    document.addEventListener('click', function (e) {
        var a = e.target.closest ? e.target.closest('a[href="/logout"]') : null;
        if (a) leaving = true;
    }, true);

    // Closing the tab records the exact "last seen" time
    window.addEventListener('pagehide', function () {
        if (!leaving && navigator.sendBeacon) navigator.sendBeacon('/api/leave');
    });

    // Tell the server the user is still working (only if they did something)
    setInterval(function () {
        if (!pending) return;
        pending = false;
        fetch('/api/heartbeat', { method: 'POST', credentials: 'same-origin' })
            .then(function (r) {
                if (r.status === 401 || r.redirected) { leaving = true; location.href = '/login'; }
            })
            .catch(function () {});
    }, HEARTBEAT_MS);

    function showBanner() {
        if (!banner) {
            banner = document.createElement('div');
            banner.textContent = '⏳ No activity detected. You will be signed out in 30 seconds. Tap or move the mouse to stay signed in.';
            banner.style.cssText = 'position:fixed;left:50%;bottom:20px;transform:translateX(-50%);' +
                'background:#f39c12;color:#000;padding:12px 18px;border-radius:10px;font-size:14px;' +
                'font-weight:600;z-index:9999;max-width:90%;text-align:center;box-shadow:0 6px 20px rgba(0,0,0,.5)';
            document.body.appendChild(banner);
        }
        banner.style.display = 'block';
    }
    setInterval(function () {
        var idle = Date.now() - lastInteraction;
        if (idle >= IDLE_MS) { leaving = true; location.href = '/logout?reason=idle'; }
        else if (idle >= WARN_MS) { showBanner(); }
    }, 5000);

    // Admin only: live "users active" badge in the sidebar
    var badge = null, notAdmin = false;
    function refreshOnline() {
        if (notAdmin) return;
        fetch('/api/online', { credentials: 'same-origin' })
            .then(function (r) {
                if (r.status === 403) { notAdmin = true; return null; }
                return r.ok ? r.json() : null;
            })
            .then(function (d) {
                if (!d) return;
                var logo = document.querySelector('.sidebar-logo');
                if (!logo) return;
                if (!badge) {
                    badge = document.createElement('a');
                    badge.href = '/staff/attendance/report';
                    badge.style.cssText = 'display:block;margin-top:10px;padding:6px 10px;' +
                        'background:#0d2a1a;border:1px solid #2a5a2a;border-radius:8px;' +
                        'color:#00ff88;font-size:0.78rem;text-decoration:none;';
                    logo.appendChild(badge);
                }
                badge.innerHTML = '🟢 <b>' + d.count + '</b> user' + (d.count === 1 ? '' : 's') + ' active';
                badge.title = d.users.map(function (u) { return u.full_name; }).join(', ');
            })
            .catch(function () {});
    }
    refreshOnline();
    setInterval(refreshOnline, ONLINE_MS);
})();