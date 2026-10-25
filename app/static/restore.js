// Copies a recovery code from the URL fragment (/restore#code=…) into the form. Fragments never reach the server.
(function () {
  var m = location.hash.match(/code=([^&]+)/);
  if (m) {
    document.getElementById("code").value = decodeURIComponent(m[1]);
    history.replaceState(null, "", location.pathname);
  }
})();
