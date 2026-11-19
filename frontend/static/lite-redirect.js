// The capability redirect of the SPA (EF-LEC-010): loaded only by a browser that does not run
// module scripts (`<script nomodule>`). It leaves for the server-rendered pages.
location.replace('/lite');
