import requests

API_KEY = "5970669b3ffb4c578374c07f92941049"

def abstract_verify(email):
    try:
        response = requests.get(
            "https://emailvalidation.abstractapi.com/v1/",
            params={
                "api_key": API_KEY,
                "email": email
            },
            timeout=10
        )

        print("STATUS:", response.status_code)

        if response.status_code != 200:
            print("API ERROR:", response.text)
            return None

        data = response.json()

        return {
            "deliverability": data.get("deliverability"),
            "quality_score": float(data.get("quality_score", 0) or 0),

            "is_valid_format":
                data.get("is_valid_format", {}).get("value"),

            "is_free_email":
                data.get("is_free_email", {}).get("value"),

            "is_disposable":
                data.get("is_disposable_email", {}).get("value"),

            "mx_found":
                data.get("is_mx_found", {}).get("value"),

            "smtp_valid":
                data.get("is_smtp_valid", {}).get("value"),

            "domain":
                data.get("domain"),

            "raw_response":
                data
        }

    except requests.Timeout:
        print("Abstract API Timeout")
        return None

    except Exception as e:
        print("Abstract API Error:", str(e))
        return None