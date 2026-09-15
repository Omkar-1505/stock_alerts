self.addEventListener('push', function(event) {
    // 1. Guaranteed fallback to prevent Chrome "Site updated in background" spam
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

    // 2. Absolute URL guarantees the OS lock-screen can find the icon.png
    const baseUrl = self.location.origin;

    const options = {
        body: payload.body,
        icon: baseUrl + '/static/icon.png',
        badge: baseUrl + '/static/badge.png',
        vibrate: [200, 100, 200, 100, 200],
        tag: 'stockpulse-stream',
        renotify: true,
        data: {
            url: payload.url || '/'
        }
    };

    // 3. MUST call showNotification to avoid Chrome penalty
    event.waitUntil(
        self.registration.showNotification(payload.title, options)
    );
});

self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    const targetUrl = event.notification.data.url;
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



// self.addEventListener('push', function(event) {
//     if (!event.data) return;
    
//     let payload;
//     try {
//         payload = event.data.json();
//     } catch (e) {
//         payload = { title: "StockPulse Alert", body: event.data.text() };
//     }

//     const options = {
//         body: payload.body,
//         icon: '/static/icon-192.png',
//         badge: '/static/badge-72.png',
//         vibrate: [200, 100, 200, 100, 200],
//         tag: 'stockpulse-alert-' + Date.now(),
//         renotify: true,
//         data: {
//             dateOfArrival: Date.now(),
//             url: '/'
//         }
//     };

//     event.waitUntil(
//         self.registration.showNotification(payload.title, options)
//     );
// });

// self.addEventListener('notificationclick', function(event) {
//     event.notification.close();
//     event.waitUntil(
//         clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
//             for (let i = 0; i < clientList.length; i++) {
//                 let client = clientList[i];
//                 if (client.url.includes('/') && 'focus' in client) {
//                     return client.focus();
//                 }
//             }
//             if (clients.openWindow) {
//                 return clients.openWindow('/');
//             }
//         })
//     );
// });