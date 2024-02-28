:html_theme.sidebar_secondary.remove:
{{ fullname | escape | underline}}

.. autoclass:: {{ fullname }}
   :members:
   :inherited-members:
   :show-inheritance:
   :undoc-members:
   :member-order: bysource
   :exclude-members: __abs__, __add__, __and__, __dir__, __eq__, __floordiv__, __ge__, __get_validators__, __gt__, __hash__, __iadd__, __iand__, __ifloordiv__, __ilshift__, __imod__, __imul__, __init__, __init_subclass__, __invert__, __iter__, __ior__, __ipow__, __irshift__, __isub__, __ixor__, __itruediv__, __le__, __lshift__, __lt__, __mod__, __modify_schema__, __mul__, __neg__, __or__, __pos__, __pow__, __pretty__, __radd__, __rand__, __rfloordiv__, __repr_name__, __rich_repr__, __rmod__, __rmul__, __ror__, __rpow__, __rshift__, __rsub__, __rtruediv__, __rxor__, __setattr__, __sub__, __truediv__, __try_update_forward_refs__, __xor__, Config

   {% block attributes %}
   {% if attributes %}
   .. rubric:: Attributes

   .. autosummary::
      :toctree:
      {% for item in attributes %}
      {% if item not in inherited_members %}
        {{ item }}
      {% endif %}
      {%- endfor %}
      {% endif %}
      {% endblock %}

   {% block methods %}
   {% if methods %}
   .. rubric:: Methods

   .. autosummary::
       :toctree:
       {% for item in methods %}
          {% if item not in inherited_members %}
            {{ item }}
          {% endif %}
       {%- endfor %}
       {% endif %}
       {% endblock %}


   .. rubric:: Common

   .. include:: ../_custom_autosummary/{{ fullname }}.rst