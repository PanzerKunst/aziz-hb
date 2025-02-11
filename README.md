# AZIZ Savings bot

This bot is the Hummingbot implementation of the PineScript strategy written in
[/scripts/savings.ps](https://github.com/PanzerKunst/aziz-hb/blob/main/scripts/savings.ps)

This strategy is a [V2 script](https://hummingbot.org/scripts/) but written according to a custom framework, as it extends
an intermediate [PkStrategy class](https://github.com/PanzerKunst/aziz-hb/blob/main/scripts/pk/pk_strategy.py)
The main strategy code is in [/scripts/savings.py](https://github.com/PanzerKunst/aziz-hb/blob/main/scripts/savings.py).
The Python source for generating Yaml configuration files is [/scripts/savings_config.py](https://github.com/PanzerKunst/aziz-hb/blob/main/scripts/savings_config.py).
One Yaml file is already generated: [/conf/scripts/conf_savings_SOL.yml](https://github.com/PanzerKunst/aziz-hb/blob/main/conf/scripts/conf_savings_SOL.yml)

## Installation

Make sure you have Conda installed (for example via the _miniconda_ library)

Then:
```
git clone https://github.com/PanzerKunst/aziz-hb.git
cd aziz-hb
./install
conda activate hummingbot
./compile
```

## Connection to trading exchange

```
cd aziz-hb
conda activate hummingbot
./start
```

If it's the first start, you have to set a password. Rather set a super simple password (1 letter?), as you'll have to
input it every time you start Hummingbot.

The [default configuration file](https://github.com/PanzerKunst/aziz-hb/blob/main/conf/scripts/conf_savings_SOL.yml)
trades on the Hyperliquid perpetual exchange (`connector_name: hyperliquid_perpetual`). You have to configure your
Hummingbot instance to be able to trade on that same exchange.

This is done via the `connect` command:
```
connect hyperliquid_perpetual
```
...and input your credentials.

Verify that the `balance` command runs correctly, and that the table under line `hyperliquid_perpetual:` displays a
positive balance. Under that table, lines `Total:` and `Exchanges Total:` may very well display zero, this seems to be
a display bug.

## Launching the bot

Hummingbot strategies take a Yaml file as parameter when run. To start the bot with the default Yaml file:
```
start --script savings.py --conf conf_savings_SOL.yml
```

Once the bot is running, you can see the live status via `status --live`.
