# Changelog

## v6.0.0
- Fix de traduction du bot
## v1.5.4
- Amélioration de l'interface utilisateur.
- Ajout de l'équipement automatique des modificateurs lors de la reconnexion.
- Ajout d'une option permettant de rejoindre un serveur VIP lors de la reconnexion.
## Dernière version (v1.5.1)
- Correction et amélioration du système de sécurité (Failsafe).
- Correction et amélioration considérable de la macro de raid.
- Ajout de légères animations dans l'interface (activables dans les paramètres de l'interface).
- Ajout de la fonctionnalité de reconnexion :
### Reconnexion
- Rejoint automatiquement la partie lorsque le jeu se déconnecte, lorsque le système Failsafe échoue ou lorsque la commande Discord !reconnect est utilisée (à des fins de test).
- Nécessite une configuration manuelle en fournissant toutes les images requises ainsi que les emplacements des clics.
- La reconnexion attend que le jeu soit entièrement chargé et effectue plusieurs tentatives si nécessaire.
- Crée automatiquement un salon, démarre la partie et permet à la macro de reprendre son fonctionnement normalement.
## v1.5.0 - Mise à jour majeure
- Possibilité d'enregistrer vos propres macros personnalisées.
- Ajout d'un créateur de macros personnalisées utilisant une syntaxe simplifiée.
- Possibilité de créer des macros intelligentes grâce à la détection d'images, aux conditions et aux délais personnalisables.
- Possibilité de partager vos macros avec d'autres utilisateurs.
## v1.4.6
- Nouvelle correction de la configuration universelle.
- Ajout d'une option Discord permettant d'utiliser des commandes à distance.
- Les commandes du bot sont :
  - !screenshot : prendre une capture d'écran.
  - !start : démarrer la macro.
  - !stop : arrêter la macro.
  - !pause : mettre la macro en pause.
- Correction espérée des fausses détections de virus (?).
## v1.4.5
- Rétablissement de l'ancienne interface utilisateur.
- Ajout d'une option permettant de réduire l'interface à une petite barre située en haut de l'écran.
- Légère amélioration de l'interface graphique.
## v1.4.4
- Amélioration du système de notifications via les Webhooks Discord.
- Remerciements particuliers à Hemo pour son aide.
## v1.4.3 - Correctif

- Modification du générateur de fichiers .exe afin d'éviter les fausses détections de virus.

## v1.4.2 - Correctif
- Correction de la macro Blood Awakening.
- Ajout d'un correctif pour les utilisateurs rencontrant des problèmes avec le verrouillage de la caméra (Shift Lock) au démarrage.
## v1.4.1
- Amélioration et correction de toutes les configurations.
- Ajout d'un serveur Discord.
- Ajout d'une section regroupant les guides.
- Amélioration de l'interface graphique.
- Amélioration du système de notifications via les Webhooks Discord.
## v1.4.0

### Raid féminin (Female Raid)
- Nécessite les capacités suivantes : Atomic Armageddon, Divine Decree, Blood Awakening et 15th Commander.
- Configurez la macro dans les paramètres. Elle ne fonctionnera pas tant que les emplacements des boutons des coffres n'auront pas été configurés.
- Si les boutons ou les récompenses ne sont pas détectés, importez vos propres images dans settings/config/Override-Asset.
### Butin (Loot)
- Ajout de compteurs de sérums (Attaque, Armure, Féminin, Bête et Colossal) pour les missions et les raids.
- Possibilité de suivre les vaisseaux pendant les raids.
- Ajout d'une section « Détails » contenant les informations relatives aux fragments et aux sérums.
- Si les éléments ne sont pas détectés, importez vos propres images dans settings/config/Override-Asset.
### Interface utilisateur (UI)
- L'éditeur de ressources dispose désormais de deux onglets : Interface et Butin.
- Ajout de l'historique des modifications (Changelog) dans le pied de page.
- Ajout de quatre nouveaux thèmes : Crimson, Mono, Gold et Midnight.
## v1.3.6
- Ajout d'un mode rapide (Fast Mode) dans la modification des configurations afin d'accélérer les opérations.
- Ajout d'une option permettant de ne plus utiliser Order Advance. Pour démarrer la macro avec une autre capacité, remplacez l'image mission_start.png par une capture de Shifting ou de Founder's Order.
## v1.3.5

### Modification des configurations (Edit Config)
- Possibilité de modifier les délais d'exécution de chaque configuration selon vos préférences.
- Cette fonctionnalité sera davantage développée à l'avenir, avec encore plus de possibilités de personnalisation.
### Remplacement des ressources (Asset Override)
- Si les images ne sont pas détectées, vous pouvez importer vos propres captures d'écran.
- La macro fonctionnera avec tous les paramètres graphiques et toutes les résolutions, y compris en plein écran (grâce à la fonctionnalité Asset Override).
### Amélioration du système de sécurité (Failsafe)
- Personnalisation complète de la zone que la macro doit viser ainsi que du temps d'attente avant le déclenchement du système de sécurité.
- Cette fonctionnalité bénéficiera de nouvelles améliorations à l'avenir.