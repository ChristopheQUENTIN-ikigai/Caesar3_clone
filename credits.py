"""Credits text for the splash-screen credits panel.

Kept in its own module so contributors can update the text without
touching the rendering code in `game_window.py`. The renderer reads
`CREDITS_TEXT` and feeds it to a single `arcade.Text` block — line
breaks are honoured (`multiline=True`).

When the credits grow (more contributors, more asset attributions,
library license text), consider switching to a structured format
(list of (heading, body) tuples) so the renderer can lay out
sections with visual hierarchy. Until then the flat string is
sufficient and trivially editable.
"""
from __future__ import annotations


CREDITS_TEXT = (
    "Caesar III Clone\n"
    "An open-source city-builder learning project in 2026 , work in progress !!! Feel free to fork it !\n\n"
    "Inspired by Caesar III (Impressions Games / Sierra, 1998).\n"
    "Splash image: ... coming soon \n\n"
    "Built with Python 3.11+ and Arcade 3.x.\n\n"
    "Christophe QUENTIN , helped by Claude 4.7 , and image generator https://deepai.org/machine-learning-model/text2img and ChatGPT image \n\n"
    "visit my github to find other projects and ideas : https://github.com/christopheQUENTIN-ikigai \n\n"
    "If your are interested in biology, biotech, medical science also visit https://exosome.hospital/   \n\n"
    " https://christophequentin.substack.com/   \n\n"    
    "Click anywhere to return."
)
