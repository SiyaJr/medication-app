// static/js/main.js
function updateCountdown() {
    const meds = document.querySelectorAll(".med-schedule");
    if (!meds.length) return;

    const now = new Date();
    let nextTime = null;

    meds.forEach(el => {
        const timeStr = el.dataset.time;
        if (!timeStr) return;

        const [hours, minutes] = timeStr.split(':').map(Number);
        let nextDate = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hours, minutes, 0);

        if (now > nextDate) {
            nextDate.setDate(nextDate.getDate() + 1); // Tomorrow
        }

        if (!nextTime || nextDate < nextTime) {
            nextTime = nextDate;
        }
    });

    if (nextTime) {
        const countdownEl = document.getElementById("countdown");

        function tick() {
            const now = new Date();
            const diff = nextTime - now;

            if (diff <= 0) {
                countdownEl.textContent = "⏰ It's time to take your medicine!";
                return;
            }

            const hours = Math.floor(diff / (1000 * 60 * 60));
            const minutes = Math.floor((diff / (1000 * 60)) % 60);
            const seconds = Math.floor((diff / 1000) % 60);

            countdownEl.textContent = `${hours}h ${minutes}m ${seconds}s`;
        }

        tick();
        setInterval(tick, 1000);
    }
}

document.addEventListener("DOMContentLoaded", updateCountdown);