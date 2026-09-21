self.addEventListener('push', function(event) {
    let payload = { 
        title: "StockPulse Active", 
        body: "Market scanner is actively monitoring your targets.", 
        url: '/' 
    };

    if (event.data) {
        try {
            payload = event.data.json();
        } catch (e) {
            payload.body = event.data.text();
        }
    }

    const baseUrl = self.location.origin;

    const options = {
        body: payload.body,
        icon: baseUrl + '/static/icon.png?v=3',
        // Uses the icon image for the badge with cache-busting to prevent white box artifacts
        badge: baseUrl + '/static/icon.png?v=3',
        vibrate: [200, 100, 200, 100, 200],
        tag: 'stockpulse-stream',
        renotify: true,
        data: {
            url: payload.url || '/'
        }
    };

    event.waitUntil(
        self.registration.showNotification(payload.title, options)
    );
});

self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    const targetUrl = event.notification.data.url || '/';
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
            for (let i = 0; i < clientList.length; i++) {
                let client = clientList[i];
                if (client.url.includes(targetUrl) && 'focus' in client) {
                    return client.focus();
                }
            }
            if (clients.openWindow) {
                return clients.openWindow(targetUrl);
            }
        })
    );
});