$(document).ready(function() {
  // Hide "New Review Request" link
  $('#navbar a[href="/r/new/"]').parent('li').hide();

  // Fix logout url
  $("a[href='/account/logout/']").attr("href", "/mozreview/logout/");
  $("#accountnav li").css("visibility", "visible");
});