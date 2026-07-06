/* Copyright: Ankitects Pty Ltd and contributors
 * License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html */

/* eslint
@typescript-eslint/no-unused-vars: "off",
*/

let time: number; // set in python code
let timerStopped = false;
let timeboxElapsed = 0;
let timeboxLimit = 0;
let timeboxReps = 0;

let maxTime = 0;

function formatTime(seconds: number): string {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    const sStr = String(s).padStart(2, "0");
    return `${m}:${sStr}`;
}

function updateTime(): void {
    const timeNode = document.getElementById("time");
    if (maxTime === 0) {
        timeNode.textContent = "";
        return;
    }
    time = Math.min(maxTime, time);
    const timeString = formatTime(time);

    if (maxTime === time) {
        timeNode.innerHTML = `<font color=red>${timeString}</font>`;
    } else {
        timeNode.textContent = timeString;
    }
}

function setTimeboxProgress(elapsed: number, limit: number, reps = 0): void {
    timeboxElapsed = elapsed;
    timeboxLimit = limit;
    timeboxReps = reps;
    updateTimeboxProgress();
}

function updateTimeboxProgress(): void {
    const container = document.getElementById("timebox-progress");
    const progressNode = document.querySelector<HTMLElement>("#timebox-progress div");
    const summaryNode = document.getElementById("timebox-summary");
    if (!container || !progressNode || !summaryNode) {
        return;
    }

    container.toggleAttribute("hidden", timeboxLimit === 0);

    if (timeboxLimit === 0) {
        progressNode.style.width = "0";
        summaryNode.textContent = "";
        return;
    }

    const remaining = Math.max(timeboxLimit - timeboxElapsed, 0);
    const progress = remaining / timeboxLimit;
    const hasPace = timeboxReps > 0 && timeboxElapsed > 0;
    const pace = hasPace ? timeboxElapsed / timeboxReps : 0;
    const paceText = hasPace ? `${Math.round(pace)} s/card` : "-- s/card";
    const expectedText = hasPace ? String(Math.floor(timeboxLimit / pace)) : "--";

    progressNode.style.width = `${progress * 100}%`;
    summaryNode.textContent = `${formatTime(remaining)} (${timeboxReps} revs., Avg: ${paceText}, Exp: ${expectedText})`;
}

let intervalId: number | undefined;

function showQuestion(txt: string, maxTime_: number): void {
    showAnswer(txt);
    time = 0;
    maxTime = maxTime_;
    updateTime();

    if (intervalId !== undefined) {
        clearInterval(intervalId);
    }

    intervalId = setInterval(function () {
        if (timeboxLimit !== 0) {
            timeboxElapsed += 1;
            updateTimeboxProgress();
        }
        if (!timerStopped) {
            time += 1;
            updateTime();
        }
    }, 1000);
}

function showAnswer(txt: string, stopTimer = false): void {
    document.getElementById("middle").innerHTML = txt;
    timerStopped = stopTimer;
}

function selectedAnswerButton(): string {
    const node = document.activeElement as HTMLElement;
    if (!node) {
        return;
    }
    return node.dataset.ease;
}
