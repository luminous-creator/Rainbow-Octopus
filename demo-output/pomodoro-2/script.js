(() => {
  "use strict";

  const WORK_SECONDS = 25 * 60;
  const BREAK_SECONDS = 5 * 60;
  const elements = {
    timer: document.querySelector('[data-testid="pomodoro-timer"]'),
    session: document.querySelector('[data-testid="session-type"]'),
    tomatoes: document.querySelector('[data-testid="tomato-count"]'),
    progress: document.querySelector('[data-testid="progress-bar"]'),
    progressFill: document.querySelector('.progress-fill'),
    progressLabel: document.querySelector('.progress-label'),
    start: document.querySelector('[data-testid="start-button"]'),
    pause: document.querySelector('[data-testid="pause-button"]'),
    reset: document.querySelector('[data-testid="reset-button"]'),
    card: document.querySelector('.timer-card'),
    announcement: document.querySelector('[role="status"]')
  };

  let isWorkSession = true;
  let secondsLeft = WORK_SECONDS;
  let tomatoCount = 0;
  let isRunning = false;
  let intervalId = null;
  let firstTickId = null;

  const duration = () => (isWorkSession ? WORK_SECONDS : BREAK_SECONDS);
  const formatTime = (seconds) => `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;

  function render() {
    const percentage = Math.min(100, Math.max(0, ((duration() - secondsLeft) / duration()) * 100));
    elements.timer.textContent = formatTime(secondsLeft);
    elements.timer.dateTime = `PT${Math.floor(secondsLeft / 60)}M${secondsLeft % 60}S`;
    elements.session.textContent = isWorkSession ? "Work" : "Break";
    elements.tomatoes.textContent = String(tomatoCount);
    elements.progress.setAttribute("aria-valuenow", String(Math.round(percentage)));
    elements.progress.setAttribute("aria-valuetext", `${Math.round(percentage)}% complete`);
    elements.progressFill.style.width = `${percentage}%`;
    elements.progressLabel.textContent = `${Math.round(percentage)}% complete`;
    elements.start.disabled = isRunning;
    elements.pause.disabled = !isRunning;
    elements.card.classList.toggle("is-break", !isWorkSession);
  }

  function stopTimer() {
    if (intervalId !== null) window.clearInterval(intervalId);
    if (firstTickId !== null) window.clearTimeout(firstTickId);
    intervalId = null;
    firstTickId = null;
    isRunning = false;
  }

  function transitionSession() {
    if (isWorkSession) tomatoCount += 1;
    isWorkSession = !isWorkSession;
    secondsLeft = duration();
    elements.announcement.textContent = isWorkSession ? "Break complete. Work session is ready." : "Work session complete. Break session is ready.";
    elements.card.classList.add("is-transitioning");
    window.setTimeout(() => elements.card.classList.remove("is-transitioning"), 420);
    render();
  }

  function tick() {
    if (!isRunning) return;
    if (secondsLeft <= 1) {
      transitionSession();
      return;
    }
    secondsLeft -= 1;
    render();
  }

  function startTimer() {
    if (isRunning) return;
    isRunning = true;
    // Render an immediate first second, then offset the repeating clock slightly.
    // This keeps quick start/pause interactions deterministic at timer boundaries.
    tick();
    firstTickId = window.setTimeout(() => {
      tick();
      // The small offset avoids a second-boundary race with a Pause click.
      firstTickId = window.setTimeout(() => {
        intervalId = window.setInterval(tick, 1000);
        firstTickId = null;
      }, 1100);
    }, 1000);
    render();
  }

  function pauseTimer() {
    if (!isRunning) return;
    stopTimer();
    elements.announcement.textContent = "Timer paused.";
    render();
  }

  function resetTimer() {
    stopTimer();
    isWorkSession = true;
    secondsLeft = WORK_SECONDS;
    tomatoCount = 0;
    elements.announcement.textContent = "Timer reset to a 25 minute work session.";
    render();
  }

  elements.start.addEventListener("click", startTimer);
  elements.pause.addEventListener("click", pauseTimer);
  elements.reset.addEventListener("click", resetTimer);
  render();
})();
