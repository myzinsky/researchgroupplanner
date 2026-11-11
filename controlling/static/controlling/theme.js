/* Charts need resolved colours; timelines can also consume CSS variables. */
(function () {
    'use strict';

    function color(role) {
        return getComputedStyle(document.documentElement).getPropertyValue('--gp-' + role).trim();
    }

    function rgb(hex) {
        return [1, 3, 5].map(function (start) { return parseInt(hex.slice(start, start + 2), 16); });
    }

    function luminance(hex) {
        var linear = rgb(hex).map(function (value) {
            value /= 255;
            return value <= 0.04045 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4);
        });
        return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
    }

    function contrast(first, second) {
        var a = luminance(first), b = luminance(second);
        return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    }

    function percentageColor(percentage) {
        var ratio = Math.max(0, Math.min(100, Number(percentage) || 0)) / 100;
        var start = rgb(color('primary-soft')), end = rgb(color('primary'));
        return '#' + start.map(function (value, index) {
            return Math.round(value + (end[index] - value) * ratio).toString(16).padStart(2, '0');
        }).join('');
    }

    function percentageStyle(percentage) {
        var background = percentageColor(percentage);
        var foreground = color('foreground'), surface = color('background');
        var text = contrast(background, foreground) >= contrast(background, surface) ? foreground : surface;
        return 'background-color: ' + background + '; border-color: ' + color('primary') + '; color: ' + text + ';';
    }

    window.gpTheme = { color: color, percentageColor: percentageColor, percentageStyle: percentageStyle };
})();
