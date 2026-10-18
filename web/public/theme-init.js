// 挂载前读取主题，避免暗色下白屏闪烁；默认跟随系统偏好。
//
// 为什么是独立文件而不是 index.html 里的内联脚本：内联脚本会逼着 CSP 放开
// `script-src`（hash/nonce），把最强的 XSS 缓解手段换掉一次主题初始化，不划算。
// 放在 <head> 里的同步脚本同样是渲染阻塞的，不会闪烁。
(function () {
  var stored = localStorage.getItem("theme");
  var dark =
    stored === "dark" ||
    (!stored && window.matchMedia("(prefers-color-scheme: dark)").matches);
  if (dark) document.documentElement.classList.add("dark");
})();
