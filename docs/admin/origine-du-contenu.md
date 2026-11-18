# Origine séparée pour le contenu des livres (facultatif)

Par défaut, les octets d'un livre sortent sur **le même domaine** que Shelfhold, sous `/content/…`,
avec `Content-Security-Policy: sandbox` sur chaque réponse : rien à faire. Les adresses `/content/…`
portent un **ticket** qui dure dix minutes, est lié à votre profil et à votre session, et que le
serveur revérifie à chaque requête (un livre reclassé ou un plafond abaissé coupe l'accès au
prochain octet). Ces adresses n'apparaissent jamais dans les journaux.

Vous pouvez servir ces octets depuis **un sous-domaine** : le cookie de session n'y est alors jamais
envoyé, et ouvrir une adresse de contenu dans un onglet reste isolé par l'origine elle-même.

1. Créez un nom (par exemple `contenu.example.org`) qui mène au même Shelfhold : DNS, certificat TLS
   et proxy inverse sont à votre charge, comme pour `SHELFHOLD_PUBLIC_URL`.
2. Définissez `SHELFHOLD_CONTENT_ORIGIN=https://contenu.example.org` (schéma et nom, sans chemin) et
   gardez `SHELFHOLD_PUBLIC_URL` : seule cette origine-là est admise par CORS, et jamais avec
   des identifiants.
3. Redémarrez. Sans cette variable, rien ne change.

Ce que cela n'apporte pas : le rendu dans le lecteur. Les pages sont lues par le navigateur puis
affichées depuis des objets `blob:` de la page de Shelfhold ; l'isolement de l'EPUB repose sur les
filtres et la politique de sécurité du lecteur, pas sur l'origine. Les clés d'API et les jetons
d'appareil sont refusés sur `/content`.
