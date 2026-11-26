// Service Worker に足す部分（vite.config.ts の workbox.importScripts）: 調査の知らせを押したら、開いている画面に戻る。
// 画面が閉じていれば、その調査を開く（panels/notify.ts が data.url に入れる）
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/ui/";
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const open = wins.find((c) => new URL(c.url).pathname.startsWith("/ui/"));
    if (open) return open.focus();
    return self.clients.openWindow(url);
  })());
});
