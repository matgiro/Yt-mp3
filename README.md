# YT MP3

Application web pour CasaOS : colle une liste de chansons (`Artiste - Titre`) ou un lien de playlist / album
**Spotify, Deezer ou YouTube**, et récupère des MP3 tagués (artiste, titre, album, pochette) directement dans
la bibliothèque Navidrome, avec la playlist `.m3u` correspondante.

## Installation depuis l'interface CasaOS (sans SSH)

1. CasaOS → **App Store** → bouton **+** en haut à droite → **Installation personnalisée** (Custom Install).
2. Clique sur l'icône **Importer** en haut à droite de la fenêtre.
3. Colle le contenu de [`casaos/yt-mp3.yml`](casaos/yt-mp3.yml) → **Valider** → **Installer**.
4. L'icône **YT MP3** apparaît sur le tableau de bord (port 8095).

## Choisir où est stockée la musique

Dans l'appli, clique sur **⚙ Réglages** (en haut) :
- **Dossier de la musique** : bouton *Parcourir…* pour naviguer dans `/DATA` (stockage CasaOS) et `/media`
  (disques USB / externes), créer un dossier si besoin, puis *Choisir ce dossier* → *Enregistrer*.
- **Organisation** : `Artiste/Album/` (par défaut), `Artiste/`, ou tout à plat.
- **Numéro dans le nom du fichier** : n° de piste de l'album (par défaut), position dans la playlist, ou aucun.
- **Sous-dossier des playlists** `.m3u` (par défaut `Playlists`).

Le dossier choisi doit être dans la bibliothèque de Navidrome. Par défaut : `/DATA/Media/Music`.

## Mise à jour

L'image `ghcr.io/matgiro/yt-mp3:latest` est reconstruite à chaque push et chaque lundi (dernier yt-dlp).
Dans CasaOS : clic droit / menu de l'app → **Vérifier les mises à jour**, ou redémarrer l'app
(yt-dlp se met aussi à jour au démarrage du conteneur).

## Formats acceptés

- `Artiste - Titre`, `Artiste - Titre - Album`, `Titre`, URL de vidéo YouTube
- Liens Spotify (playlist, album, titre), Deezer (playlist, album, titre), YouTube / YouTube Music (playlist, album)
- Fichiers `.txt`, `.m3u`, `.csv` (dont l'export [Exportify](https://exportify.net) de Spotify)
