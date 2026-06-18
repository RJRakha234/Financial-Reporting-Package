"""mailtrigger — run a report when a specific email arrives in Outlook.

Pipeline:  watch inbox  ->  match trigger email  ->  fetch report from a URL
(with parameters)  ->  process the response  ->  output the result.

Everything is driven by a single ``config.yaml`` so the same tool can be
pointed at any sender, any URL, and any parameters.
"""

__version__ = "0.1.0"
