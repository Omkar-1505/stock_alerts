self.addEventListener('push', function(event) {
    if (!event.data) return;
    
    let payload;
    try {
        payload = event.data.json();
    } catch (e) {
        payload = { title: "StockPulse Alert", body: event.data.text() };
    }

    const options = {
        body: payload.body,
        icon: '/static/icon-192.png',
        badge: '/static/badge-72.png',
        vibrate: [200, 100, 200, 100, 200],
        tag: 'stockpulse-alert-' + Date.now(),
        renotify: true,
        data: {
            dateOfArrival: Date.now(),
            url: '/'
        }
    };

    event.waitUntil(
        self.registration.showNotification(payload.title, options)
    );
});

self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
            for (let i = 0; i < clientList.length; i++) {
                let client = clientList[i];
                if (client.url.includes('/') && 'focus' in client) {
                    return client.focus();
                }
            }
            if (clients.openWindow) {
                return clients.openWindow('/');
            }
        })
    );
});