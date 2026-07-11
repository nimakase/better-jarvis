// Web Push 订阅：授权一次后，OS 级通知（日报/清单/报错/休假确认）即可送达。
(function () {
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) return;

  function urlB64ToUint8Array(b64) {
    const pad = '='.repeat((4 - (b64.length % 4)) % 4);
    const s = (b64 + pad).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(s);
    return Uint8Array.from([...raw].map(c => c.charCodeAt(0)));
  }

  async function subscribe() {
    const reg = await navigator.serviceWorker.register('/sw.js').catch(() => navigator.serviceWorker.ready);
    await navigator.serviceWorker.ready;
    const r = await fetch('/api/push/vapid-public');
    const { key } = await r.json();
    if (!key) { console.warn('VAPID 未配置，跳过推送订阅'); return false; }
    const sub = await (await navigator.serviceWorker.ready).pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlB64ToUint8Array(key),
    });
    await fetch('/api/push/subscribe', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(sub),
    });
    return true;
  }

  function showEnableButton() {
    if (document.getElementById('jarvis-push-btn')) return;
    const b = document.createElement('button');
    b.id = 'jarvis-push-btn';
    b.textContent = '🔔 开启通知';
    b.style.cssText = 'position:fixed;right:16px;bottom:84px;z-index:9999;background:#1c2030;color:#e6e8ee;' +
      'border:1px solid #242a3a;border-radius:999px;padding:9px 14px;font-size:13px;cursor:pointer;' +
      'box-shadow:0 4px 16px rgba(0,0,0,.35);';
    b.onclick = async () => {
      const perm = await Notification.requestPermission();
      if (perm === 'granted') { try { await subscribe(); b.remove(); } catch (e) { console.warn(e); } }
      else { b.remove(); }
    };
    document.body.appendChild(b);
  }

  window.addEventListener('load', () => {
    setTimeout(() => {
      if (Notification.permission === 'granted') subscribe().catch(e => console.warn('push', e));
      else if (Notification.permission === 'default') showEnableButton();
    }, 1500);
  });
})();
