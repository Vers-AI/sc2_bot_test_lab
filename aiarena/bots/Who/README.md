Made with [ares-sc2 framework](https://github.com/AresSC2/ares-sc2)  
[Bot repo](https://github.com/raspersc2/who)

Update submodules:

`git submodule update --init --recursive --remote`

---
# who?
There exists a variety of sharp, sometimes gimmicky strategies that tend to appear and disappear on 
ladder. This bot is meant as a test opponent that uses those kinds of builds, 
so bots can practice against them. This is particularly helpful when preparing for tournaments.

The bot is open source and designed to work with the 
[local play bootstrap docker image](https://github.com/aiarena/local-play-bootstrap) so you can practise 
against specific builds locally.

Please don’t reupload this bot directly to the ladder. You’re more than welcome, though, 
to use it as a resource or inspiration when creating your own bot.

## who's included?

- [really (P)](https://aiarena.net/bots/997/)
- [why (T)](https://aiarena.net/bots/954/)
- [what (Z)](https://aiarena.net/bots/934/)

## testing
This bot can be downloaded and used for testing via the [local play bootstrap docker image](https://github.com/aiarena/local-play-bootstrap).

Inside `config.yml` you can set the race by changing the `MyBotRace` value.

Edit `terran_builds.yml`, `protoss_builds.yml` or `zerg_builds.yml` file as appropiate to test a specific build.

Feel free to reach out to me on discord if you need help setting it up.
