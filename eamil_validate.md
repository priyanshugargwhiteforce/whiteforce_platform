URL : https://astro-buddy.in/django/api/batch-validate/

Payload (Body-raw-jason):
(

    {
  "emails": [
    "harshnigam207@gmail.com",
    "princekashyap91@outlook.com",
    "shainki@white-force.in",
    "harshnigam.whiteforce@gmail.com",
    "kaleenbhaiya208@gmail.com"
  ]
}
)

Response:
(
   { 
    "success": true,
    "total": 5,
    "processing_time_seconds": 9.7,
    "cache_hits": 0,
    "freshly_validated": 5,
    "threads_used": 5,
    "average_time_per_email": 1.94,
    "summary": {
        "deliverable": 4,
        "risky": 0,
        "invalid": 1,
        "unknown": 0,
        "errors": 0
    },
    "results": [
        {
            "email": "harshnigam207@gmail.com",
            "status": "deliverable",
            "valid": true,
            "score": 85,
            "mx": true,
            "spf": true,
            "dkim": true,
            "dmarc": true,
            "smtp": {
                "smtp_valid": null,
                "smtp_status": "smtp_skipped_provider_policy"
            },
            "catchall": false,
            "disposable": false,
            "role_account": false,
            "random_username": false,
            "typo": false,
            "reputation": 20,
            "domain_age": 20
        },
        {
            "email": "princekashyap91@outlook.com",
            "status": "undeliverable",
            "valid": false,
            "score": 0,
            "mx": true,
            "spf": true,
            "dkim": true,
            "dmarc": true,
            "smtp": {
                "smtp_valid": false,
                "smtp_status": "mailbox_does_not_exist",
                "smtp_code": 550,
                "smtp_message": "5.5.0 Requested action not taken: mailbox unavailable (S2017062302). [BN2PEPF000055E0.namprd21.prod.outlook.com 2026-08-05T12:37:17.626Z 08DEEF08480B4F2B]",
                "mx_server": "outlook-com.olc.protection.outlook.com"
            },
            "catchall": false,
            "disposable": false,
            "role_account": false,
            "random_username": false,
            "typo": false,
            "reputation": 20,
            "domain_age": 20
        },
        {
            "email": "kaleenbhaiya208@gmail.com",
            "status": "deliverable",
            "valid": true,
            "score": 85,
            "mx": true,
            "spf": true,
            "dkim": true,
            "dmarc": true,
            "smtp": {
                "smtp_valid": null,
                "smtp_status": "smtp_skipped_provider_policy"
            },
            "catchall": false,
            "disposable": false,
            "role_account": false,
            "random_username": false,
            "typo": false,
            "reputation": 20,
            "domain_age": 20
        },
        {
            "email": "shainki@white-force.in",
            "status": "deliverable",
            "valid": true,
            "score": 90,
            "mx": true,
            "spf": true,
            "dkim": false,
            "dmarc": false,
            "smtp": {
                "smtp_valid": true,
                "smtp_status": "mailbox_exists_likely",
                "smtp_code": 250,
                "smtp_message": "Recipient <shainki@white-force.in> OK",
                "mx_server": "mx.zoho.in"
            },
            "catchall": false,
            "disposable": false,
            "role_account": false,
            "random_username": false,
            "typo": false,
            "reputation": 10,
            "domain_age": 15
        },
        {
            "email": "harshnigam.whiteforce@gmail.com",
            "status": "deliverable",
            "valid": true,
            "score": 85,
            "mx": true,
            "spf": true,
            "dkim": true,
            "dmarc": true,
            "smtp": {
                "smtp_valid": null,
                "smtp_status": "smtp_skipped_provider_policy"
            },
            "catchall": false,
            "disposable": false,
            "role_account": false,
            "random_username": false,
            "typo": false,
            "reputation": 20,
            "domain_age": 20
        }
    ]
}
)



# For Streaming the emails validation Responses 

URl : https://astro-buddy.in/django/api/batch-validate-stream/

Payload : Payload (Body-raw-jason):
(

    {
  "emails": [
    "harshnigam207@gmail.com",
    "princekashyap91@outlook.com",
    "shainki@white-force.in",
    "harshnigam.whiteforce@gmail.com",
    "kaleenbhaiya208@gmail.com"
  ]
}
)