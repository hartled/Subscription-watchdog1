"""Cancel-guide library. Links point to official pages (checked Oct 2026). Steps change often;
each guide says so and links to the provider's own help page."""
from __future__ import annotations

from .normalize import slugify

APP_STORE_NOTE = ("If you subscribed through Apple (iPhone/iPad) or Google Play, cancel there instead — "
                  "the service's own website may not show the subscription.")

GUIDES: dict[str, dict] = {}


def _g(name, steps, url, help_url=None, notes=None, billed_via_store=True):
    GUIDES[slugify(name)] = {
        "name": name, "slug": slugify(name), "steps": steps, "url": url, "help_url": help_url,
        "notes": (notes or []) + ([APP_STORE_NOTE] if billed_via_store else []),
    }


_g("Netflix", ["Sign in at netflix.com in a browser.", "Open Account → Membership.", "Choose Cancel membership and confirm.",
               "Netflix keeps access until the end of the billing period."],
   "https://www.netflix.com/cancelplan", "https://help.netflix.com/en/node/407",
   ["If billed through a mobile carrier or cable provider, cancel with that company."])
_g("Spotify", ["Sign in at spotify.com/account (the desktop/mobile app can't cancel).", "Go to Manage your plan.",
               "Choose Cancel Premium and confirm.", "You drop to Spotify Free at the end of the billing period."],
   "https://www.spotify.com/account/", "https://support.spotify.com/us/article/cancel-premium/")
_g("Hulu", ["Sign in at hulu.com and open your Account page.", "Under Your Subscription, select Cancel.",
            "Decline the retention offers and confirm."],
   "https://secure.hulu.com/account", "https://help.hulu.com/article/hulu-cancel-subscription",
   ["If Hulu is part of a Disney bundle or billed by a partner (e.g. a carrier), cancel through that bundle/partner."])
_g("Disney+", ["Sign in at disneyplus.com.", "Select your profile → Account.", "Select your Disney+ subscription → Cancel Subscription.",
               "Complete the confirmation."], "https://www.disneyplus.com/account", "https://help.disneyplus.com")
_g("Amazon Prime", ["Sign in at amazon.com.", "Open Account → Prime membership (amazon.com/mc).",
                    "Select Manage membership → End membership.", "Follow the prompts; Amazon shows several 'are you sure' pages — keep going until you see a confirmation."],
   "https://www.amazon.com/mc", "https://www.amazon.com/yourmembershipsandsubscriptions",
   ["Prime Video channels and other Amazon subscriptions are listed separately under Memberships & Subscriptions."],
   billed_via_store=False)
_g("Kindle Unlimited", ["Sign in at amazon.com.", "Open Memberships & Subscriptions → Kindle Unlimited.",
                        "Select Cancel Kindle Unlimited Membership."],
   "https://www.amazon.com/kindle-dbs/ku/ku-central", "https://www.amazon.com/yourmembershipsandsubscriptions", billed_via_store=False)
_g("Audible", ["Sign in at audible.com (cancellation isn't available in the iOS app).", "Open Account Details.",
               "Select Cancel membership and complete the steps.", "Use your remaining credits first — they're lost after cancelling."],
   "https://www.audible.com/account/overview")
_g("Apple", ["iPhone/iPad: Settings → [your name] → Subscriptions.", "Tap the subscription → Cancel Subscription.",
             "Mac: App Store → your name → Account Settings → Subscriptions → Manage.",
             "Charges labelled APPLE.COM/BILL can be iCloud+, Apple Music, Apple TV+, Apple One, or third-party app subscriptions — check the list."],
   "https://support.apple.com/en-us/118428", billed_via_store=False)
_g("Adobe", ["Sign in at account.adobe.com/plans.", "Select Manage plan → Cancel your plan.", "Follow the prompts.",
             "Annual plans billed monthly can carry an early-termination fee — Adobe shows the amount before you confirm."],
   "https://account.adobe.com/plans")
_g("Microsoft 365", ["Sign in at account.microsoft.com/services.", "Find the subscription → Manage.",
                     "Select Cancel subscription (or turn off recurring billing)."], "https://account.microsoft.com/services")
_g("Xbox Game Pass", ["Sign in at account.microsoft.com/services with your Xbox account.", "Find Game Pass → Manage.",
                      "Select Cancel subscription or turn off recurring billing."], "https://account.microsoft.com/services")
_g("YouTube Premium", ["Go to youtube.com/paid_memberships while signed in.", "Select Manage membership → Deactivate/Cancel.",
                       "Confirm."], "https://www.youtube.com/paid_memberships")
_g("Google Play / Google One", ["Open play.google.com/store/account/subscriptions (or Play Store app → profile → Payments & subscriptions → Subscriptions).",
                                "Pick the subscription (Google One, app subscriptions, etc.) → Cancel subscription.",
                                "Follow the prompts."],
   "https://play.google.com/store/account/subscriptions", "https://support.google.com/googleplay/answer/7018481", billed_via_store=False)
_g("HBO Max", ["Sign in on the web and open your profile → Subscription.", "Select Cancel Your Subscription.",
               "If billed by a partner (Amazon, Apple, a cable/phone provider), cancel with that partner."],
   "https://help.hbomax.com")
_g("Paramount+", ["Sign in at paramountplus.com and open Account.", "Select Cancel Subscription.", "Confirm."],
   "https://www.paramountplus.com/account/")
_g("Peacock", ["Sign in at peacocktv.com and open Account → Plans & Payment.", "Select Change Plan / Cancel Plan.", "Confirm."],
   "https://www.peacocktv.com/account")
_g("Dropbox", ["Sign in at dropbox.com on the web.", "Open Settings → Plan.", "Select Cancel plan and confirm.",
               "Your files stay, but you're limited to the free storage quota."], "https://www.dropbox.com/account/plan")
_g("New York Times", ["Sign in at nytimes.com and open Account → Subscription overview.",
                      "Select Cancel subscription (in many states this is available online; otherwise use chat).",
                      "Keep the confirmation email."], "https://help.nytimes.com")
_g("ChatGPT", ["Web: open chatgpt.com → your profile → Settings → Account / Subscription.", "Select Manage → Cancel plan.",
               "If you subscribed in the iOS/Android app, cancel through Apple or Google Play."], "https://help.openai.com")
_g("PlayStation Plus", ["On console: Settings → Users and Accounts → Account → Payment and Subscriptions → Subscriptions.",
                        "Select PlayStation Plus → Turn Off Auto-Renew.", "Or sign in to your account on playstation.com and do the same under Subscriptions."],
   "https://www.playstation.com/en-us/support/subscriptions/cancel-playstation-plus/", billed_via_store=False)
_g("Nintendo Switch Online", ["Sign in at accounts.nintendo.com.", "Open Shop Menu → Nintendo Switch Online → Turn off automatic renewal.",
                              "Or on the console: Nintendo eShop → your profile → Nintendo Switch Online → Turn off automatic renewal."],
   "https://accounts.nintendo.com", billed_via_store=False)
_g("LinkedIn Premium", ["Desktop: click Me → Premium features → Manage Premium account.", "Select Cancel subscription and follow the steps.",
                        "If purchased through the iOS app, cancel via Apple."], "https://www.linkedin.com/help/linkedin")
_g("SiriusXM", ["Sign in at the SiriusXM account center (care.siriusxm.com) and look for the cancel option, or use online chat.",
                "Phone cancellation is also available; expect retention offers — say 'please cancel' clearly.",
                "Ask for a confirmation number and keep it."], "https://care.siriusxm.com", billed_via_store=False)
_g("Planet Fitness", ["Check your home club's policy — many clubs require cancelling in person or by mailed letter; some states require online cancellation.",
                      "Visit your home club (or send a certified letter) and request cancellation in writing.",
                      "Get a written confirmation and watch for the annual fee charge."], "https://www.planetfitness.com", billed_via_store=False)
_g("DoorDash DashPass", ["In the DoorDash app: Account → Manage DashPass.", "Select End Subscription and confirm."],
   "https://help.doordash.com")
_g("Uber One", ["In the Uber app: Account → Uber One → Manage membership.", "Select End membership and confirm."], "https://help.uber.com")
_g("Peloton", ["Sign in at onepeloton.com and open Settings → Subscriptions.", "Select the membership → Cancel subscription."],
   "https://www.onepeloton.com")
_g("Duolingo", ["Web: open duolingo.com/settings/subscription.", "Select Cancel subscription.",
                "If you subscribed in the iOS/Android app, cancel through Apple or Google Play."],
   "https://www.duolingo.com/settings/subscription", billed_via_store=False)
_g("Canva", ["Sign in at canva.com on the web → Settings → Billing & plans.", "Select your plan → Cancel subscription."],
   "https://www.canva.com/help/")

GENERIC = {
    "name": "Any other subscription", "slug": "generic", "url": None, "help_url": None,
    "steps": [
        "Identify the merchant: search the charge description (e.g. 'SQ *ACME 800-555…') — the phone number or domain in it often names the company.",
        "Check app-store billing first: Apple (Settings → your name → Subscriptions) and Google Play (Play Store → Payments & subscriptions).",
        "Check PayPal: Settings → Payments → Manage automatic payments.",
        "Sign in to the service's website and look under Account, Billing, Plan or Membership for Cancel / Turn off auto-renew.",
        "No online option? Use chat or email so there's a written record; ask for a confirmation number.",
        "Watch your next statement. If you're still charged after cancelling, contact the merchant, then your card issuer — you can ask them to stop future recurring charges and dispute unauthorised ones.",
        "Tip: card issuers that offer virtual card numbers let you lock or close a number used for a subscription.",
    ],
    "notes": [
        "US federal law (ROSCA) and many state auto-renewal laws (e.g. California) require clear disclosures and a simple way to cancel online subscriptions. The FTC's 2024 'click-to-cancel' rule was vacated by a federal court in 2025, so rights vary by state.",
        "This is general information, not legal advice.",
    ],
    "links": [("Apple subscriptions", "https://support.apple.com/en-us/118428"),
              ("Google Play subscriptions", "https://play.google.com/store/account/subscriptions"),
              ("PayPal automatic payments", "https://www.paypal.com/myaccount/autopay/")],
}


def guide_for(merchant: str) -> dict:
    return GUIDES.get(slugify(merchant), GENERIC)
