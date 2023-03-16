# Modern Media Server Setup

This project is a modern way to automate your home media server by using several services such as Jellyfin, Radarr, Readarr, Jackett, and qBittorrent. The services are deployed using Docker containers, which makes it easy to manage and scale the services as needed.

## Requirements
---
To use this project, you will need:

-   Docker and Docker Compose installed on your system.
-   A media library that you want to manage using the services listed above.
-   Basic understanding of Docker and Docker Compose.

## Installation
---
To get started with this project, follow these steps:

1.  Clone the project repository to your local system:
    
``` bash
git clone https://github.com/yourusername/media-server.git
```    
1.  Navigate to the project directory:
    
``` bash
cd media-server
```
       
1.  Edit the `docker-compose.yml` file and configure the services to your liking. For example, you can specify the directory where your media library is stored and the credentials for the services.
    
4.  Start the services using Docker Compose:

``` docker
docker compose build && docker compose up -d
```
This will start all the services specified in the docker-compose.yml file and run them in detached mode.
    
5.  Access the services by visiting the following URLs:
    
    -   Jellyfin: [http://localhost:8096](http://localhost:8096/)
    -   Radarr: [http://localhost:7878](http://localhost:7878/)
    -   Readarr: [http://localhost:8787](http://localhost:8787/)
    -   Jackett: [http://localhost:9117](http://localhost:9117/)
    -   qBittorrent: [http://localhost:8080](http://localhost:8080/)
    
    Note that the URLs may vary depending on the configuration you have specified in the `docker-compose.yml` file.
    

## Usage
---
Once the services are up and running, you can start managing your media library using the web interfaces of the services. Here's a brief overview of what each service does:

-   Jellyfin: A media server that allows you to stream movies, TV shows, music, and other media files to your devices.
-   Radarr: A movie management tool that automatically searches for and downloads movies based on your preferences.
-   Readarr: A book management tool that automatically searches for and downloads books based on your preferences.
-   Jackett: A torrent indexer that allows you to search for torrents on multiple torrent websites.
-   qBittorrent: A BitTorrent client that allows you to download and manage torrents.

You can configure each service according to your preferences and manage your media library using these tools.

## Conclusion
---
By using this project, you can automate your media server setup and manage your media library with ease. Docker makes it easy to deploy and manage these services, and the web interfaces of the services make it easy to manage your media library.