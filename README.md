[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

# I need IMAX Seats (╯°□°)╯︵ ┻━┻

# IMAX Seats Notifier

This Python script monitors a [Traumpalast Website](https://leonberg.traumpalast.de/) and sends a notification to a Discord channel whenever the content of the website changes.

## Prerequisites

You need Python 3.x and pip (Python package installer) installed on your system. If you don't have Python installed you can find it [here](https://www.python.org/downloads/). 

This script also uses several Python packages:

- `requests`
- `beautifulsoup4`
- `discord-webhook`
- `python-dotenv`

## Setup Instructions

1. Clone this repository to your local machine:

    ```
    git clone https://github.com/xD33m/imax-seats.git
    ```

2. Navigate to the project directory:

    ```
    cd imax-seats
    ```

3. Install the required packages:

    ```
    pip install -r requirements.txt
    ```

4. Set up your `.env` file:

    Create a new file in the project directory and name it `.env`. Define your environment variables like this:

    ```
    TARGET_URL=https://your-target-website.com
    DISCORD_WEBHOOK_URL=Your Discord Webhook URL
    ```

    Replace `https://your-target-website.com` and `Your Discord Webhook URL` with your actual target URL and Discord Webhook URL, respectively.

5. Run the script:

    ```
    python notifier.py
    ```

## Usage

The script will continuously monitor the specified website and send a message to the specified Discord channel whenever the content of the website changes.

## License

MIT License
