# AlternanceAssistant

Panel web local pour piloter ta recherche d'alternance.

## Lancer
Double-clique sur **`Lancer AlternanceAssistant.bat`** → le navigateur s'ouvre sur http://127.0.0.1:5055.
Ferme la fenêtre noire pour arrêter.

## Première configuration (onglet Paramètres)
1. Ton nom, ta signature, ton profil (utilisé par l'IA).
2. **Gmail** : active la validation en 2 étapes sur ton compte Google, puis crée un
   mot de passe d'application sur https://myaccount.google.com/apppasswords et colle-le.
   Bouton « Envoyer un mail de test » pour vérifier.
3. Ajoute ton **CV** (PDF) en pièce jointe.
4. (Optionnel) une clé API Anthropic pour la réécriture des mails par IA.

## Onglets
- **Tableau de bord** : stats, pipeline, relances dues, prochaines entreprises à démarcher.
- **Index entreprises** : tout ce qui est démarché / à démarcher, recherche, filtre par statut, import/export CSV.
- **Relances** : à faire maintenant (en retard / aujourd'hui) et à venir. Rédiger, marquer comme relancé, reporter, stopper.
- **Assistant candidature** : choisis l'entreprise + un modèle → mail pré-rempli → réécriture IA → envoi Gmail avec CV.
  Après l'envoi, le statut passe à « Contacté » et une relance est programmée (délai réglable, 7 jours par défaut).
- **Historique mails**, **Modèles de mails** (variables `{entreprise}`, `{poste}`, `{contact}`, …).

## Données
Tout est dans le dossier `data/` (base SQLite `alternance.db`, `config.json`, `pieces_jointes/`).
Créé au premier lancement et exclu de Git (`.gitignore`) : il contient ton mot de passe d'application, ta clé API et ton CV.
Sauvegarde-le à part pour ne rien perdre.
