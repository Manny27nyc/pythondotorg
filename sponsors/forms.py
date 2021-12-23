from itertools import chain
from django import forms
from django.conf import settings
from django.contrib.admin.widgets import AdminDateWidget
from django.db.models import Q
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django_countries.fields import CountryField

from sponsors.models import (
    SponsorshipBenefit,
    SponsorshipPackage,
    SponsorshipProgram,
    Sponsor,
    SponsorContact,
    Sponsorship,
    SponsorBenefit,
    SponsorEmailNotificationTemplate,
    RequiredImgAssetConfiguration,
    BenefitFeature,
)


class PickSponsorshipBenefitsField(forms.ModelMultipleChoiceField):
    widget = forms.CheckboxSelectMultiple

    def label_from_instance(self, obj):
        return obj.name


class SponsorContactForm(forms.ModelForm):
    class Meta:
        model = SponsorContact
        fields = ["name", "email", "phone", "primary", "administrative", "accounting"]


SponsorContactFormSet = forms.formset_factory(
    SponsorContactForm,
    extra=0,
    min_num=1,
    validate_min=True,
    can_delete=False,
    can_order=False,
    max_num=5,
)


class SponsorshipsBenefitsForm(forms.Form):
    """
    Form to enable user to select packages, benefits and add-ons during
    the sponsorship application submission.
    """
    package = forms.ModelChoiceField(
        queryset=SponsorshipPackage.objects.list_advertisables(),
        widget=forms.RadioSelect(),
        required=False,
        empty_label=None,
    )
    add_ons_benefits = PickSponsorshipBenefitsField(
        required=False,
        queryset=SponsorshipBenefit.objects.add_ons().select_related("program"),
    )
    a_la_carte_benefits = PickSponsorshipBenefitsField(
        required=False,
        queryset=SponsorshipBenefit.objects.a_la_carte().select_related("program"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        benefits_qs = SponsorshipBenefit.objects.with_packages().select_related(
            "program"
        )

        for program in SponsorshipProgram.objects.all():
            slug = slugify(program.name).replace("-", "_")
            self.fields[f"benefits_{slug}"] = PickSponsorshipBenefitsField(
                queryset=benefits_qs.filter(program=program),
                required=False,
                label=_("{program_name} Benefits").format(program_name=program.name),
            )

    @property
    def benefits_programs(self):
        return [f for f in self if f.name.startswith("benefits_")]

    @property
    def benefits_conflicts(self):
        """
        Returns a dict with benefits ids as keys and their list of conlicts ids as values
        """
        conflicts = {}
        for benefit in SponsorshipBenefit.objects.with_conflicts():
            benefits_conflicts = benefit.conflicts.values_list("id", flat=True)
            if benefits_conflicts:
                conflicts[benefit.id] = list(benefits_conflicts)
        return conflicts

    def get_benefits(self, cleaned_data=None, include_add_ons=False, include_a_la_carte=False):
        cleaned_data = cleaned_data or self.cleaned_data
        benefits = list(
            chain(*(cleaned_data.get(bp.name) for bp in self.benefits_programs))
        )
        add_ons = cleaned_data.get("add_ons_benefits", [])
        if include_add_ons:
            benefits.extend([b for b in add_ons])
        a_la_carte = cleaned_data.get("a_la_carte_benefits", [])
        if include_a_la_carte:
            benefits.extend([b for b in a_la_carte])
        return benefits

    def get_package(self):
        return self.cleaned_data.get("package")

    def _clean_benefits(self, cleaned_data):
        """
        Validate chosen benefits. Invalid scenarios are:
        - benefits with conflits
        - package only benefits and form without SponsorshipProgram
        - benefit with no capacity, except if soft
        """
        package = cleaned_data.get("package")
        benefits = self.get_benefits(cleaned_data, include_add_ons=True)
        a_la_carte = cleaned_data.get("a_la_carte_benefits")

        if not benefits and not a_la_carte:
            raise forms.ValidationError(
                _("You have to pick a minimum number of benefits.")
            )
        elif benefits and not package:
            raise forms.ValidationError(
                _("You must pick a package to include the selected benefits.")
            )

        benefits_ids = [b.id for b in benefits]
        for benefit in benefits:
            conflicts = set(self.benefits_conflicts.get(benefit.id, []))
            if conflicts and set(benefits_ids).intersection(conflicts):
                raise forms.ValidationError(
                    _("The application has 1 or more benefits that conflicts.")
                )

            if benefit.package_only:
                if not package:
                    raise forms.ValidationError(
                        _(
                            "The application has 1 or more package only benefits and no sponsor package."
                        )
                    )
                elif not benefit.packages.filter(id=package.id).exists():
                    raise forms.ValidationError(
                        _(
                            "The application has 1 or more package only benefits but wrong sponsor package."
                        )
                    )

            if not benefit.has_capacity:
                raise forms.ValidationError(
                    _("The application has 1 or more benefits with no capacity.")
                )

        return cleaned_data

    def clean(self):
        cleaned_data = super().clean()
        return self._clean_benefits(cleaned_data)


class SponsorshipApplicationForm(forms.Form):
    name = forms.CharField(
        max_length=100,
        label="Sponsor name",
        help_text="Name of the sponsor, for public display.",
        required=False,
    )
    description = forms.CharField(
        label="Sponsor description",
        help_text="Brief description of the sponsor for public display.",
        required=False,
        widget=forms.TextInput,
    )
    landing_page_url = forms.URLField(
        label="Sponsor landing page",
        help_text="Landing page URL. The linked page may not contain any sales or marketing information.",
        required=False,
    )
    twitter_handle = forms.CharField(
        max_length=32,
        label="Twitter handle",
        help_text="For promotion of your sponsorship on social media.",
        required=False,
    )
    web_logo = forms.ImageField(
        label="Sponsor web logo",
        help_text="For display on our sponsor webpage. High resolution PNG or JPG, smallest dimension no less than 256px",
        required=False,
    )
    print_logo = forms.ImageField(
        label="Sponsor print logo",
        help_text="For printed materials, signage, and projection. SVG or EPS",
        required=False,
    )

    primary_phone = forms.CharField(
        label="Sponsor Primary Phone",
        max_length=32,
        required=False,
    )
    mailing_address_line_1 = forms.CharField(
        label="Mailing Address line 1",
        widget=forms.TextInput,
        required=False,
    )
    mailing_address_line_2 = forms.CharField(
        label="Mailing Address line 2",
        widget=forms.TextInput,
        required=False,
    )

    city = forms.CharField(max_length=64, required=False)
    state = forms.CharField(
        label="State/Province/Region", max_length=64, required=False
    )
    postal_code = forms.CharField(
        label="Zip/Postal Code", max_length=64, required=False
    )
    country = CountryField().formfield(required=False)

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        qs = Sponsor.objects.none()
        if self.user:
            sponsor_ids = SponsorContact.objects.filter(user=self.user).values_list(
                "sponsor", flat=True
            )
            qs = Sponsor.objects.filter(id__in=sponsor_ids)
        self.fields["sponsor"] = forms.ModelChoiceField(queryset=qs, required=False)

        formset_kwargs = {"prefix": "contact"}
        if self.data:
            self.contacts_formset = SponsorContactFormSet(self.data, **formset_kwargs)
        else:
            self.contacts_formset = SponsorContactFormSet(**formset_kwargs)

    def clean(self):
        cleaned_data = super().clean()
        sponsor = self.data.get("sponsor")
        if not sponsor and not self.contacts_formset.is_valid():
            msg = "Errors with contact(s) information"
            if not self.contacts_formset.errors:
                msg = "You have to enter at least one contact"
            raise forms.ValidationError(msg)
        elif not sponsor:
            has_primary_contact = any(
                f.cleaned_data.get("primary") for f in self.contacts_formset.forms
            )
            if not has_primary_contact:
                msg = "You have to mark at least one contact as the primary one."
                raise forms.ValidationError(msg)

    def clean_sponsor(self):
        sponsor = self.cleaned_data.get("sponsor")
        if not sponsor:
            return

        if Sponsorship.objects.in_progress().filter(sponsor=sponsor).exists():
            msg = f"The sponsor {sponsor.name} already have open Sponsorship applications. "
            msg += f"Get in contact with {settings.SPONSORSHIP_NOTIFICATION_FROM_EMAIL} to discuss."
            raise forms.ValidationError(msg)

        return sponsor

    # Required fields are being manually validated because if the form
    # data has a Sponsor they shouldn't be required
    def clean_name(self):
        name = self.cleaned_data.get("name", "")
        sponsor = self.data.get("sponsor")
        if not sponsor and not name:
            raise forms.ValidationError("This field is required.")
        return name.strip()

    def clean_web_logo(self):
        web_logo = self.cleaned_data.get("web_logo", "")
        sponsor = self.data.get("sponsor")
        if not sponsor and not web_logo:
            raise forms.ValidationError("This field is required.")
        return web_logo

    def clean_primary_phone(self):
        primary_phone = self.cleaned_data.get("primary_phone", "")
        sponsor = self.data.get("sponsor")
        if not sponsor and not primary_phone:
            raise forms.ValidationError("This field is required.")
        return primary_phone.strip()

    def clean_mailing_address_line_1(self):
        mailing_address_line_1 = self.cleaned_data.get("mailing_address_line_1", "")
        sponsor = self.data.get("sponsor")
        if not sponsor and not mailing_address_line_1:
            raise forms.ValidationError("This field is required.")
        return mailing_address_line_1.strip()

    def clean_city(self):
        city = self.cleaned_data.get("city", "")
        sponsor = self.data.get("sponsor")
        if not sponsor and not city:
            raise forms.ValidationError("This field is required.")
        return city.strip()

    def clean_postal_code(self):
        postal_code = self.cleaned_data.get("postal_code", "")
        sponsor = self.data.get("sponsor")
        if not sponsor and not postal_code:
            raise forms.ValidationError("This field is required.")
        return postal_code.strip()

    def clean_country(self):
        country = self.cleaned_data.get("country", "")
        sponsor = self.data.get("sponsor")
        if not sponsor and not country:
            raise forms.ValidationError("This field is required.")
        return country.strip()

    def save(self):
        selected_sponsor = self.cleaned_data.get("sponsor")
        if selected_sponsor:
            return selected_sponsor

        sponsor = Sponsor.objects.create(
            name=self.cleaned_data["name"],
            web_logo=self.cleaned_data["web_logo"],
            primary_phone=self.cleaned_data["primary_phone"],
            mailing_address_line_1=self.cleaned_data["mailing_address_line_1"],
            mailing_address_line_2=self.cleaned_data.get("mailing_address_line_2", ""),
            city=self.cleaned_data["city"],
            state=self.cleaned_data.get("state", ""),
            postal_code=self.cleaned_data["postal_code"],
            country=self.cleaned_data["country"],
            description=self.cleaned_data.get("description", ""),
            landing_page_url=self.cleaned_data.get("landing_page_url", ""),
            twitter_handle=self.cleaned_data["twitter_handle"],
            print_logo=self.cleaned_data.get("print_logo"),
        )
        contacts = [f.save(commit=False) for f in self.contacts_formset.forms]
        for contact in contacts:
            if self.user and self.user.email.lower() == contact.email.lower():
                contact.user = self.user
            contact.sponsor = sponsor
            contact.save()

        return sponsor

    @cached_property
    def user_with_previous_sponsors(self):
        if not self.user:
            return False
        return self.fields["sponsor"].queryset.exists()


class SponsorshipReviewAdminForm(forms.ModelForm):
    start_date = forms.DateField(widget=AdminDateWidget(), required=False)
    end_date = forms.DateField(widget=AdminDateWidget(), required=False)

    def __init__(self, *args, **kwargs):
        force_required = kwargs.pop("force_required", False)
        super().__init__(*args, **kwargs)
        if force_required:
            for field_name in self.fields:
                self.fields[field_name].required = True

    class Meta:
        model = Sponsorship
        fields = ["start_date", "end_date", "package", "sponsorship_fee"]

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get("start_date")
        end_date = cleaned_data.get("end_date")

        if start_date and end_date and end_date <= start_date:
            raise forms.ValidationError("End date must be greater than start date")

        return cleaned_data


class SignedSponsorshipReviewAdminForm(SponsorshipReviewAdminForm):
    """
    Form to approve sponsorships that already have a signed contract
    """
    signed_contract = forms.FileField(help_text="Please upload the final version of the signed contract.")


class SponsorBenefitAdminInlineForm(forms.ModelForm):
    sponsorship_benefit = forms.ModelChoiceField(
        queryset=SponsorshipBenefit.objects.order_by('program', 'order').select_related("program"),
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    class Meta:
        model = SponsorBenefit
        fields = ["sponsorship_benefit", "sponsorship", "benefit_internal_value"]

    def save(self, commit=True):
        sponsorship = self.cleaned_data["sponsorship"]
        benefit = self.cleaned_data["sponsorship_benefit"]
        value = self.cleaned_data["benefit_internal_value"]

        if not (self.instance and self.instance.pk):  # new benefit
            self.instance = SponsorBenefit(sponsorship=sponsorship)
        else:
            self.instance.refresh_from_db()

        self.instance.benefit_internal_value = benefit.internal_value
        if value:
            self.instance.benefit_internal_value = value
        updated_sponsorship_benefit = False
        if benefit.pk != self.instance.sponsorship_benefit_id:
            updated_sponsorship_benefit = True
            self.instance.sponsorship_benefit = benefit
            self.instance.name = benefit.name
            self.instance.description = benefit.description
            self.instance.program = benefit.program

        if commit:
            self.instance.save()

            if updated_sponsorship_benefit:
                self.instance.features.all().delete()
                for feature_config in benefit.features_config.all():
                    feature_config.create_benefit_feature(self.instance)

        return self.instance


class SponsorshipsListForm(forms.Form):
    sponsorships = forms.ModelMultipleChoiceField(
        required=True,
        queryset=Sponsorship.objects.select_related("sponsor"),
        widget=forms.CheckboxSelectMultiple,
    )

    @classmethod
    def with_benefit(cls, sponsorship_benefit, *args, **kwargs):
        """
        Queryset considering only valid sponsorships which have the benefit
        """
        today = timezone.now().date()
        queryset = sponsorship_benefit.related_sponsorships.exclude(
            Q(end_date__lt=today) | Q(status=Sponsorship.REJECTED)
        ).select_related("sponsor")

        form = cls(*args, **kwargs)
        form.fields["sponsorships"].queryset = queryset
        form.sponsorship_benefit = sponsorship_benefit

        return form


class SendSponsorshipNotificationForm(forms.Form):
    contact_types = forms.MultipleChoiceField(
        choices=SponsorContact.CONTACT_TYPES,
        required=True,
        widget=forms.CheckboxSelectMultiple,
    )
    notification = forms.ModelChoiceField(
        queryset=SponsorEmailNotificationTemplate.objects.all(),
        help_text="You can select an existing notification or your own custom subject/content",
        required=False,
    )
    subject = forms.CharField(max_length=140, required=False)
    content = forms.CharField(widget=forms.widgets.Textarea(), required=False)

    def clean(self):
        cleaned_data = super().clean()
        notification = cleaned_data.get("notification")
        subject = cleaned_data.get("subject", "").strip()
        content = cleaned_data.get("content", "").strip()
        custom_notification = subject or content

        if not (notification or custom_notification):
            raise forms.ValidationError("Can not send email without notification or custom content")
        if notification and custom_notification:
            raise forms.ValidationError("You must select a notification or use custom content, not both")

        return cleaned_data

    def get_notification(self):
        default_notification = SponsorEmailNotificationTemplate(
            content=self.cleaned_data["content"],
            subject=self.cleaned_data["subject"],
        )
        return self.cleaned_data.get("notification") or default_notification


class SponsorUpdateForm(forms.ModelForm):
    READONLY_FIELDS = [
        "name",
    ]

    web_logo = forms.ImageField(
        widget=forms.widgets.FileInput,
        help_text="For display on our sponsor webpage. High resolution PNG or JPG, smallest dimension no less than 256px",
        required=False,
    )
    print_logo = forms.ImageField(
        widget=forms.widgets.FileInput,
        help_text="For printed materials, signage, and projection. SVG or EPS",
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        formset_kwargs = {"prefix": "contact", "instance": self.instance}
        factory = forms.inlineformset_factory(
            Sponsor,
            SponsorContact,
            form=SponsorContactForm,
            extra=0,
            min_num=1,
            validate_min=True,
            can_delete=True,
            can_order=False,
            max_num=5,
        )
        if self.data:
            self.contacts_formset = factory(self.data, **formset_kwargs)
        else:
            self.contacts_formset = factory(**formset_kwargs)
        # display fields as read-only
        for disabled in self.READONLY_FIELDS:
            self.fields[disabled].widget.attrs['readonly'] = True

    class Meta:
        exclude = ["created", "updated", "creator", "last_modified_by"]
        model = Sponsor

    def clean(self):
        cleaned_data = super().clean()

        if not self.contacts_formset.is_valid():
            msg = "Errors with contact(s) information"
            if not self.contacts_formset.errors:
                msg = "You have to enter at least one contact"
            raise forms.ValidationError(msg)

        has_primary_contact = any(
            f.cleaned_data.get("primary") for f in self.contacts_formset.forms
        )
        if not has_primary_contact:
            msg = "You have to mark at least one contact as the primary one."
            raise forms.ValidationError(msg)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.contacts_formset.save()


class RequiredImgAssetConfigurationForm(forms.ModelForm):

    def clean(self):
        data = super().clean()

        min_width, max_width = data.get("min_width"), data.get("max_width")
        if min_width and max_width and max_width < min_width:
            raise forms.ValidationError("Max width must be greater than min width")
        min_height, max_height = data.get("min_height"), data.get("max_height")
        if min_height and max_height and max_height < min_height:
            raise forms.ValidationError("Max height must be greater than min height")

        return data

    class Meta:
        model = RequiredImgAssetConfiguration
        fields = "__all__"


class SponsorRequiredAssetsForm(forms.Form):
    """
    This form is used by the sponsor to fullfill their information related
    to the required assets. The form is built dynamically by fetching the
    required assets from the sponsorship.
    """

    def __init__(self, *args, **kwargs):
        """
        Init method introspect the sponsorship object and
        build the form object
        """
        self.sponsorship = kwargs.pop("instance", None)
        required_assets_ids = kwargs.pop("required_assets_ids", [])
        if not self.sponsorship:
            msg = "Form must be initialized with a sponsorship passed by the instance parameter"
            raise TypeError(msg)
        super().__init__(*args, **kwargs)
        self.required_assets = BenefitFeature.objects.required_assets().from_sponsorship(self.sponsorship)
        if required_assets_ids:
            self.required_assets = self.required_assets.filter(pk__in=required_assets_ids)

        fields = {}
        for required_asset in self.required_assets:
            value = required_asset.value
            f_name = self._get_field_name(required_asset)
            required = bool(value)
            fields[f_name] = required_asset.as_form_field(required=required, initial=value)

        self.fields.update(fields)

    def _get_field_name(self, asset):
        return slugify(asset.internal_name).replace("-", "_")

    def update_assets(self):
        """
        Iterate over every required asset, get the value from form data and
        update it
        """
        for req_asset in self.required_assets:
            f_name = self._get_field_name(req_asset)
            value = self.cleaned_data.get(f_name, None)
            if value is None:
                continue
            req_asset.value = value

    @property
    def has_input(self):
        return bool(self.fields)


class SponsorshipBenefitAdminForm(forms.ModelForm):

    class Meta:
        model = SponsorshipBenefit
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        a_la_carte = cleaned_data.get("a_la_carte")
        packages = cleaned_data.get("packages")

        # a la carte benefit cannot be associated with a package
        if a_la_carte and packages:
            error = "À la carte benefits must not belong to any package."
            raise forms.ValidationError(error)

        return cleaned_data
