self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_) {
    payload = { title: "Serena", body: event.data?.text() || "Job finished" };
  }

  const options = {
    body: payload.body || "Job finished",
    tag: payload.tag || "serena-job",
    data: { url: payload.url || "/dashboard/" },
    icon: "serena-icon-128.png",
  };
  event.waitUntil(self.registration.showNotification(payload.title || "Serena", options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetUrl = new URL(event.notification.data?.url || "/dashboard/", self.location.origin).href;
  const dashboardUrl = new URL("/dashboard/", self.location.origin).href;

  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      const existing = windows.find((client) => client.url.startsWith(dashboardUrl));
      if (existing) {
        return existing.navigate(targetUrl).then((client) => client?.focus());
      }
      return clients.openWindow(targetUrl);
    }),
  );
});
