/*
 * 贾维斯 PWA Service Worker
 *
 * 设计原则：贾维斯是一个「实时」应用（WebSocket 对话 + 动态 API），
 * 缓存旧内容只会带来麻烦。所以这里采用「纯网络透传」策略——
 * 不缓存任何对话/接口数据，仅为满足 PWA 可安装条件（需要一个 fetch 处理器）
 * 并对 GET 请求提供一个轻量的离线兜底。
 */

const APP_SHELL = "jarvis-shell-v2";

// 安装：立即接管，不等待旧 SW 退出
self.addEventListener("install", (event) => {
  self.skipWaiting();
});

// 激活：清理旧缓存并接管所有页面
self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((k) => k !== APP_SHELL).map((k) => caches.delete(k))
      );
      await self.clients.claim();
    })()
  );
});

// 取数：网络优先、纯透传。
// 只处理同源 GET 请求；WebSocket 升级请求与跨域请求一律放行不拦截。
self.addEventListener("fetch", (event) => {
  const req = event.request;

  // 只接管 GET，其余（POST/上传等）直接走网络，避免破坏接口语义
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // 非同源资源不拦截
  if (url.origin !== self.location.origin) return;

  // 接口与下载不缓存：始终走网络
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws")) {
    return; // 默认网络行为
  }

  event.respondWith(
    fetch(req).catch(() => caches.match(req))
  );
});

// Web Push：收到推送 → 弹 OS 级通知（页面关着也能收到）
self.addEventListener("push", (event) => {
  let d = { title: "贾维斯", body: "", url: "/" };
  try { d = Object.assign(d, event.data.json()); }
  catch (_) { if (event.data) d.body = event.data.text(); }
  event.waitUntil(
    self.registration.showNotification(d.title || "贾维斯", {
      body: d.body || "",
      icon: "/icon-192.png",
      badge: "/icon-192.png",
      data: { url: d.url || "/" },
    })
  );
});

// 点通知 → 聚焦已开窗口并跳转，或新开窗口
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
      for (const c of cs) {
        if ("focus" in c) { c.navigate(target); return c.focus(); }
      }
      return self.clients.openWindow(target);
    })
  );
});
